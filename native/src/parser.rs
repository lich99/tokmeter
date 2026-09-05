//! Source-specific normalization. No pricing, Python, network, or persistent storage.
use chrono::DateTime;
use serde::Deserialize;
use serde_json::Value;
use std::sync::Arc;

pub type Text = Arc<str>;
#[derive(Clone, Debug)]
pub struct Row {
    pub id: Text,
    pub timestamp: i64,
    pub model: Text,
    pub project: Text,
    pub source: u8,
    pub sub: bool,
    // 0 unknown, 1 standard, 2 fast, 3 priority, 4 flex.
    pub tier: u8,
    // input, cached, output (including reasoning), reasoning, write 5m, write 1h, read.
    pub tokens: [i64; 7],
    pub cumulative: Option<[i64; 5]>,
}

#[derive(Default, Clone)]
pub struct State {
    pub model: Text,
    pub project: Text,
    pub sub: bool,
    pub total: Option<[i64; 5]>,
    pub fork_time: Option<i64>,
    pub session_id: Text,
    pub ancestors: Vec<Text>,
    pub seen_meta: bool,
}

#[derive(Default, Clone, Debug, serde::Serialize)]
pub struct Diagnostics {
    pub malformed: usize,
    pub invalid_usage: usize,
    pub duplicate_snapshots: usize,
    pub inherited_events: usize,
    pub cumulative_resets: usize,
}

#[derive(Deserialize, Default)]
struct Envelope {
    #[serde(rename = "type", default)]
    kind: String,
    timestamp: Option<String>,
    cwd: Option<String>,
    payload: Option<Payload>,
    message: Option<Message>,
    #[serde(rename = "requestId")]
    request_id: Option<String>,
}
#[derive(Deserialize, Default)]
struct Payload {
    #[serde(rename = "type")]
    kind: Option<String>,
    id: Option<String>,
    session_id: Option<String>,
    timestamp: Option<String>,
    thread_source: Option<String>,
    parent_thread_id: Option<String>,
    agent_path: Option<String>,
    model: Option<String>,
    cwd: Option<String>,
    source: Option<Value>,
    agent_role: Option<String>,
    agent_nickname: Option<String>,
    forked_from_id: Option<String>,
    info: Option<Info>,
    service_tier: Option<String>,
}
#[derive(Deserialize, Default)]
struct Info {
    last_token_usage: Option<Usage>,
    total_token_usage: Option<Usage>,
    service_tier: Option<String>,
}
#[derive(Deserialize, Default)]
struct Message {
    id: Option<String>,
    model: Option<String>,
    usage: Option<Usage>,
}
#[derive(Deserialize, Default)]
struct Usage {
    input_tokens: Option<i64>,
    cached_input_tokens: Option<i64>,
    output_tokens: Option<i64>,
    reasoning_output_tokens: Option<i64>,
    total_tokens: Option<i64>,
    cache_read_input_tokens: Option<i64>,
    cache_creation_input_tokens: Option<i64>,
    cache_creation: Option<Cache>,
    service_tier: Option<String>,
    speed: Option<String>,
}
#[derive(Deserialize, Default)]
struct Cache {
    ephemeral_5m_input_tokens: Option<i64>,
    ephemeral_1h_input_tokens: Option<i64>,
}
impl Usage {
    fn cumulative(&self) -> [i64; 5] {
        [
            self.input_tokens.unwrap_or(0),
            self.cached_input_tokens.unwrap_or(0),
            self.output_tokens.unwrap_or(0),
            self.reasoning_output_tokens.unwrap_or(0),
            self.total_tokens.unwrap_or(0),
        ]
    }
}
pub fn normalize(model: &str) -> Text {
    let mut m = model.to_lowercase();
    if m.ends_with("[1m]") {
        m.truncate(m.len() - 4);
    }
    if let Some((prefix, suffix)) = m.rsplit_once('-') {
        if suffix.len() == 8 && suffix.bytes().all(|x| x.is_ascii_digit()) {
            m = prefix.to_owned();
        }
    }
    if m.is_empty() {
        "unknown".into()
    } else {
        m.into()
    }
}
fn tier(value: Option<&str>) -> u8 {
    match value {
        Some("standard" | "default") => 1,
        Some("fast") => 2,
        Some("priority") => 3,
        Some("flex") => 4,
        _ => 0,
    }
}
fn timestamp(value: Option<&str>) -> Option<i64> {
    DateTime::parse_from_rfc3339(value?)
        .ok()
        .map(|x| x.timestamp_millis())
}

pub fn parse(line: &[u8], source: u8, state: &mut State, d: &mut Diagnostics) -> Option<Row> {
    let candidate = if source == 0 {
        memchr::memmem::find(line, b"\"usage\"").is_some()
    } else {
        [
            b"\"token_count\"".as_slice(),
            b"\"turn_context\"",
            b"\"session_meta\"",
        ]
        .iter()
        .any(|s| memchr::memmem::find(line, s).is_some())
    };
    if !candidate {
        return None;
    }
    let event: Envelope = match serde_json::from_slice(line) {
        Ok(r) => r,
        Err(_) => {
            d.malformed += 1;
            return None;
        }
    };
    if source == 0 {
        return claude(event, state, d);
    }
    let p = event.payload.unwrap_or_default();
    match event.kind.as_str() {
        "session_meta" => {
            let id = p.id.or(p.session_id).unwrap_or_default();
            // Forks embed their parent's SessionMeta, with rewritten outer timestamps.
            // The first header owns this file; embedded headers must not change its role.
            if state.seen_meta {
                if !id.is_empty() && id != state.session_id.as_ref() {
                    let id: Text = id.into();
                    if !state.ancestors.contains(&id) {
                        state.ancestors.push(id);
                    }
                }
                return None;
            }
            state.seen_meta = true;
            state.session_id = id.into();
            if let Some(cwd) = p.cwd {
                state.project = cwd.into();
            }
            let nonempty = |s: &Option<String>| s.as_ref().is_some_and(|s| !s.is_empty());
            state.sub = p.thread_source.as_deref() == Some("subagent")
                || p.source.as_ref().and_then(|s| s.get("subagent")).is_some()
                || nonempty(&p.agent_role)
                || nonempty(&p.agent_nickname)
                || (p.thread_source.as_deref() != Some("user")
                    && (nonempty(&p.parent_thread_id)
                        || p.agent_path
                            .as_ref()
                            .is_some_and(|s| s.starts_with("/root/"))));
            if let Some(parent) = p.forked_from_id.filter(|s| !s.is_empty()) {
                state.ancestors.push(parent.into());
                state.fork_time = timestamp(p.timestamp.as_deref().or(event.timestamp.as_deref()));
            }
            None
        }
        "turn_context" => {
            if let Some(model) = p.model {
                state.model = normalize(&model);
            }
            if let Some(cwd) = p.cwd {
                state.project = cwd.into();
            }
            // A requested turn_context tier is not proof of the served tier.
            None
        }
        "event_msg" if p.kind.as_deref() == Some("token_count") => {
            let info = p.info?;
            let usage = info.last_token_usage.unwrap_or_default();
            let t = [
                usage.input_tokens.unwrap_or(0),
                usage.cached_input_tokens.unwrap_or(0),
                usage.output_tokens.unwrap_or(0),
                usage.reasoning_output_tokens.unwrap_or(0),
                0,
                0,
                0,
            ];
            if t.iter().any(|&v| v < 0) || t[1] > t[0] || t[3] > t[2] {
                d.invalid_usage += 1;
                return None;
            }
            let ts = timestamp(event.timestamp.as_deref())?;
            let cumulative = info.total_token_usage.map(|u| u.cumulative());
            if let Some(total) = cumulative {
                if state.total == Some(total) {
                    d.duplicate_snapshots += 1;
                    return None;
                }
                if state
                    .total
                    .is_some_and(|old| total[0] < old[0] || total[2] < old[2])
                {
                    d.cumulative_resets += 1;
                }
                state.total = Some(total);
            }
            if t[0] == 0 && t[2] == 0 {
                return None;
            }
            if state.fork_time.is_some_and(|born| ts < born) {
                d.inherited_events += 1;
                return None;
            }
            let served = tier(info.service_tier.as_deref().or(p.service_tier.as_deref()));
            Some(Row {
                id: "".into(),
                timestamp: ts,
                model: state.model.clone(),
                project: state.project.clone(),
                source,
                sub: state.sub,
                tier: served,
                tokens: t,
                cumulative,
            })
        }
        _ => None,
    }
}
fn claude(event: Envelope, state: &mut State, d: &mut Diagnostics) -> Option<Row> {
    if event.kind != "assistant" {
        return None;
    }
    let msg = event.message?;
    let usage = msg.usage?;
    let model = normalize(msg.model.as_deref().unwrap_or("unknown"));
    if &*model == "<synthetic>" || &*model == "unknown" {
        return None;
    }
    let mut t = [
        usage.input_tokens.unwrap_or(0),
        0,
        usage.output_tokens.unwrap_or(0),
        0,
        0,
        0,
        usage.cache_read_input_tokens.unwrap_or(0),
    ];
    if let Some(cache) = usage.cache_creation {
        t[4] = cache.ephemeral_5m_input_tokens.unwrap_or(0);
        t[5] = cache.ephemeral_1h_input_tokens.unwrap_or(0);
    }
    if t.iter().any(|&v| v < 0) || usage.cache_creation_input_tokens.is_some_and(|v| v < 0) {
        d.invalid_usage += 1;
        return None;
    }
    // Unattributed cache writes use the default 5m TTL; preserve known 1h writes.
    t[4] += (usage.cache_creation_input_tokens.unwrap_or(0) - t[4] - t[5]).max(0);
    if t.iter().any(|&v| v < 0) {
        d.invalid_usage += 1;
        return None;
    }
    if let Some(cwd) = event.cwd {
        state.project = cwd.into();
    }
    let id = msg.id.or(event.request_id).unwrap_or_default();
    let served = if usage.speed.as_deref() == Some("fast") {
        2
    } else {
        tier(usage.service_tier.as_deref())
    };
    Some(Row {
        id: id.into(),
        timestamp: timestamp(event.timestamp.as_deref())?,
        model,
        project: state.project.clone(),
        source: 0,
        sub: state.sub,
        tier: served,
        tokens: t,
        cumulative: None,
    })
}
