//! Append-aware memory store. One writer; readers use published Python snapshots.
use crate::parser::{self, Diagnostics, Row, State};
use serde::{Deserialize, Serialize};
use std::{
    collections::{HashMap, HashSet},
    fs::File,
    io::{self, BufRead, BufReader, Read, Seek, SeekFrom},
    sync::Mutex,
};

#[derive(Clone, Deserialize)]
pub struct Input {
    pub path: String,
    pub size: u64,
    pub identity: String,
    pub mtime_ns: u64,
    pub source: u8,
    pub project: String,
    pub sub: bool,
}
pub struct FileState {
    meta: Input,
    offset: u64,
    anchor: Vec<u8>,
    parser: State,
    rows: Vec<Row>,
    diagnostics: Diagnostics,
}
impl FileState {
    fn new(meta: Input) -> Self {
        let parser = State {
            project: meta.project.as_str().into(),
            sub: meta.sub,
            model: "unknown".into(),
            ..State::default()
        };
        Self {
            meta,
            offset: 0,
            anchor: vec![],
            parser,
            rows: vec![],
            diagnostics: Diagnostics::default(),
        }
    }
}
#[derive(Default, Serialize)]
pub struct Report {
    pub scanned: usize,
    pub changed: usize,
    pub removed: usize,
    pub bytes_read: u64,
    pub rebuilt: usize,
    pub appended: usize,
    pub malformed: usize,
    pub invalid_usage: usize,
    pub duplicate_snapshots: usize,
    pub inherited_events: usize,
    pub cumulative_resets: usize,
    pub pending_files: usize,
    pub errors: Vec<String>,
    pub updated: bool,
}
#[derive(Default)]
pub struct Engine {
    files: HashMap<String, FileState>,
    initialized: bool,
}

impl Engine {
    pub fn refresh(&mut self, inputs: Vec<Input>, workers: usize) -> Report {
        let mut report = Report {
            scanned: inputs.len(),
            ..Report::default()
        };
        let present: HashSet<_> = inputs.iter().map(|x| x.path.as_str()).collect();
        let before = self.files.len();
        self.files.retain(|path, _| present.contains(path.as_str()));
        report.removed = before - self.files.len();
        let mut jobs = vec![];
        for item in inputs {
            if self.files.get(&item.path).is_some_and(|old| {
                old.meta.identity == item.identity
                    && old.meta.size == item.size
                    && old.meta.mtime_ns == item.mtime_ns
            }) {
                continue;
            }
            let old = self.files.remove(&item.path);
            jobs.push((item, old));
        }
        report.changed = jobs.len();
        jobs.sort_by_key(|(item, old)| {
            item.size
                .saturating_sub(old.as_ref().map_or(0, |s| s.offset))
        });
        let queue = Mutex::new(jobs);
        let results = std::thread::scope(|scope| {
            let handles: Vec<_> = (0..workers.clamp(1, 32))
                .map(|_| {
                    let queue = &queue;
                    scope.spawn(move || {
                        let mut results = vec![];
                        loop {
                            let job = queue.lock().expect("job lock poisoned").pop();
                            let Some((meta, old)) = job else { break };
                            let path = meta.path.clone();
                            results.push((path, load(meta, old)));
                        }
                        results
                    })
                })
                .collect();
            handles
                .into_iter()
                .flat_map(|h| h.join().expect("parser worker panicked"))
                .collect::<Vec<_>>()
        });
        for (path, (state, result)) in results {
            if let Some(state) = state {
                self.files.insert(path.clone(), state);
            }
            match result {
                Ok((bytes, rebuilt, updated)) => {
                    report.bytes_read += bytes;
                    report.rebuilt += usize::from(rebuilt);
                    report.appended += usize::from(!rebuilt);
                    report.updated |= updated;
                }
                Err(error) => {
                    report.updated = true;
                    report.errors.push(format!("{path}: {error}"));
                }
            }
        }
        report.updated |= report.removed > 0 || !self.initialized;
        self.initialized = true;
        for f in self.files.values() {
            report.malformed += f.diagnostics.malformed;
            report.invalid_usage += f.diagnostics.invalid_usage;
            report.duplicate_snapshots += f.diagnostics.duplicate_snapshots;
            report.inherited_events += f.diagnostics.inherited_events;
            report.cumulative_resets += f.diagnostics.cumulative_resets;
            report.pending_files += usize::from(f.offset < f.meta.size);
        }
        report
    }

    pub fn export(&self) -> (serde_json::Value, Vec<u8>) {
        let mut models = vec![];
        let mut projects = vec![];
        let mut model_ids = HashMap::new();
        let mut project_ids = HashMap::new();
        let mut selected: Vec<&Row> = vec![];
        let mut messages: HashMap<&str, usize> = HashMap::new();
        let mut files: Vec<_> = self.files.iter().collect();
        files.sort_by(|a, b| a.0.cmp(b.0));
        for (_, file) in files {
            for row in &file.rows {
                if row.source == 0 && !row.id.is_empty() {
                    if let Some(&index) = messages.get(row.id.as_ref()) {
                        let old = selected[index];
                        if row.tokens[2] > old.tokens[2]
                            || (row.tokens[2] == old.tokens[2] && row.timestamp > old.timestamp)
                        {
                            selected[index] = row;
                        }
                        continue;
                    }
                    messages.insert(&row.id, selected.len());
                }
                selected.push(row);
            }
        }
        let mut bytes = Vec::with_capacity(selected.len() * 13 * 8);
        for row in &selected {
            let model = *model_ids.entry(row.model.as_ref()).or_insert_with(|| {
                let id = models.len() as i64;
                models.push(row.model.as_ref());
                id
            });
            let project = *project_ids.entry(row.project.as_ref()).or_insert_with(|| {
                let id = projects.len() as i64;
                projects.push(row.project.as_ref());
                id
            });
            for value in [
                row.timestamp,
                model,
                project,
                row.source as i64,
                i64::from(row.sub),
                row.tier as i64,
                row.tokens[0],
                row.tokens[1],
                row.tokens[2],
                row.tokens[3],
                row.tokens[4],
                row.tokens[5],
                row.tokens[6],
            ] {
                bytes.extend_from_slice(&value.to_le_bytes());
            }
        }
        (
            serde_json::json!({"schema": 1, "rows": selected.len(), "models": models, "projects": projects}),
            bytes,
        )
    }
}

type LoadResult = (Option<FileState>, io::Result<(u64, bool, bool)>);
fn load(meta: Input, old: Option<FileState>) -> LoadResult {
    let mut file = match File::open(&meta.path) {
        Ok(f) => f,
        Err(e) => return (old, Err(e)),
    };
    let mut rebuild = old.as_ref().is_none_or(|s| {
        s.meta.identity != meta.identity
            || meta.size < s.meta.size
            || (meta.size == s.meta.size && meta.mtime_ns != s.meta.mtime_ns)
    });
    if let Some(state) = &old {
        if !rebuild && !state.anchor.is_empty() {
            let mut check = vec![0; state.anchor.len()];
            rebuild = file
                .seek(SeekFrom::Start(state.offset - state.anchor.len() as u64))
                .is_err()
                || file.read_exact(&mut check).is_err()
                || check != state.anchor;
        }
    }
    let had_rows = old.as_ref().is_some_and(|s| !s.rows.is_empty());
    let mut state = if rebuild {
        FileState::new(meta.clone())
    } else {
        old.expect("existing file")
    };
    state.meta = meta;
    let result = read(&mut state, file)
        .map(|(bytes, added)| (bytes, rebuild, added || (rebuild && had_rows)));
    if result.is_err() {
        state.meta.mtime_ns = 0;
    } // Retry transient I/O failures.
    (Some(state), result)
}
fn read(state: &mut FileState, mut file: File) -> io::Result<(u64, bool)> {
    file.seek(SeekFrom::Start(state.offset))?;
    let mut reader =
        BufReader::with_capacity(256 * 1024, file.take(state.meta.size - state.offset));
    let mut line = vec![];
    let mut bytes = 0;
    let before = state.rows.len();
    loop {
        let n = reader.read_until(b'\n', &mut line)?;
        if n == 0 {
            break;
        }
        bytes += n as u64;
        if line.last() != Some(&b'\n') {
            break;
        } // Retry an incomplete tail on the next append.
        if let Some(row) = parser::parse(
            &line,
            state.meta.source,
            &mut state.parser,
            &mut state.diagnostics,
        ) {
            state.rows.push(row);
        }
        state.offset += n as u64;
        state.anchor = line[line.len().saturating_sub(64)..].to_vec();
        line.clear();
    }
    Ok((bytes, state.rows.len() != before))
}
