pub mod engine;
pub mod parser;

#[cfg(feature = "python")]
mod binding {
    use super::engine;
    use pyo3::{exceptions::PyValueError, prelude::*, types::PyBytes};
    use std::sync::Mutex;

    #[pyclass(frozen, module = "tokmeter._native")]
    pub struct Engine {
        inner: Mutex<engine::Engine>,
    }
    #[pymethods]
    impl Engine {
        #[new]
        fn new() -> Self {
            Self {
                inner: Mutex::new(engine::Engine::default()),
            }
        }
        #[pyo3(signature = (manifest, workers, force=false))]
        fn refresh(
            &self,
            py: Python<'_>,
            manifest: String,
            workers: usize,
            force: bool,
        ) -> PyResult<(String, Option<Py<PyBytes>>)> {
            let inputs = serde_json::from_str(&manifest)
                .map_err(|e| PyValueError::new_err(e.to_string()))?;
            let (metadata, buffer) = py.detach(|| {
                let mut engine = self.inner.lock().expect("engine lock poisoned");
                let mut report = engine.refresh(inputs, workers);
                if report.updated || force {
                    let (mut metadata, buffer) = engine.export();
                    engine.add_export_diagnostics(&mut report);
                    metadata["scan"] = serde_json::to_value(report).expect("serializable report");
                    (metadata.to_string(), Some(buffer))
                } else {
                    engine.add_export_diagnostics(&mut report);
                    (serde_json::json!({"scan": report}).to_string(), None)
                }
            });
            Ok((metadata, buffer.map(|b| PyBytes::new(py, &b).unbind())))
        }
    }
    #[pymodule]
    fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
        module.add_class::<Engine>()?;
        Ok(())
    }
}
