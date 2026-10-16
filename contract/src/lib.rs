//! Shared contract validation and transactional persistence. Wire types are generated.
#[path = "../generated/types.rs"]
mod wire;
use anyhow::{Result, ensure};
use axum::{
    Json,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde::{Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use std::{
    path::Path,
    sync::{Arc, Mutex, OnceLock},
};
pub use wire::*;
pub mod console;

pub const SPEC: &str = include_str!("../openapi.json");
pub fn validate(name: &str, value: &Value) -> Result<()> {
    static VALIDATORS: OnceLock<std::collections::HashMap<String, jsonschema::Validator>> =
        OnceLock::new();
    let validators = VALIDATORS.get_or_init(|| {
        let spec: Value = serde_json::from_str(SPEC).unwrap();
        spec["components"]["schemas"].as_object().unwrap().keys().map(|name| {
            let schema = json!({"$ref":format!("#/components/schemas/{name}"), "components":spec["components"]});
            (name.clone(), jsonschema::options().should_validate_formats(true).build(&schema).expect("checked contract"))
        }).collect()
    });
    let validator = validators
        .get(name)
        .ok_or_else(|| anyhow::anyhow!("unknown schema"))?;
    validator
        .validate(value)
        .map_err(|e| anyhow::anyhow!("{name}: {e}"))?;
    Ok(())
}
pub fn decode<T: DeserializeOwned>(name: &str, value: Value) -> Result<T, ApiError> {
    validate(name, &value).map_err(ApiError::bad)?;
    serde_json::from_value(value).map_err(ApiError::bad)
}
pub fn digest(value: &impl Serialize) -> String {
    rvbench_core::json::spec_digest(&serde_json::to_value(value).unwrap())
}
/// Parse the original body before schema validation; serde_json::Value alone would
/// silently keep the last value of a duplicate object key.
pub struct RawJson(pub Value);
impl<S: Send + Sync> axum::extract::FromRequest<S> for RawJson {
    type Rejection = ApiError;
    async fn from_request(
        request: axum::extract::Request,
        state: &S,
    ) -> Result<Self, Self::Rejection> {
        let bytes = axum::body::Bytes::from_request(request, state)
            .await
            .map_err(|e| ApiError(e.status(), e.to_string()))?;
        let text = std::str::from_utf8(&bytes).map_err(ApiError::bad)?;
        Ok(Self(
            rvbench_core::json::parse_json(text).map_err(ApiError::bad)?,
        ))
    }
}
pub fn id() -> String {
    uuid::Uuid::new_v4().to_string()
}
pub fn now() -> String {
    chrono::Utc::now().to_rfc3339()
}
pub fn seconds() -> f64 {
    chrono::Utc::now().timestamp_millis() as f64 / 1000.0
}
pub fn terminal(s: &JobState) -> bool {
    !matches!(s, JobState::Queued | JobState::Running)
}
pub fn record(request: JobRequest) -> JobRecord {
    JobRecord {
        phase_timings_seconds: None,
        job_id: id(),
        request,
        state: JobState::Queued,
        worker_id: String::new(),
        board_id: String::new(),
        instance_id: String::new(),
        submitted_at: now(),
        started_at: None,
        finished_at: None,
        compilation: None,
        evaluation: None,
        error: None,
        logs: String::new(),
        assignment_id: None,
        attempt_id: None,
        lease_id: None,
    }
}
#[derive(Debug)]
pub struct ApiError(pub StatusCode, pub String);
impl ApiError {
    pub fn bad(e: impl std::fmt::Display) -> Self {
        Self(StatusCode::BAD_REQUEST, e.to_string())
    }
    pub fn conflict(e: impl std::fmt::Display) -> Self {
        Self(StatusCode::CONFLICT, e.to_string())
    }
    pub fn missing() -> Self {
        Self(StatusCode::NOT_FOUND, "not found".into())
    }
}
impl From<anyhow::Error> for ApiError {
    fn from(e: anyhow::Error) -> Self {
        Self(StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
    }
}
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"error":self.1}))).into_response()
    }
}

/// One atomic control-state snapshot per service. SQLite WAL + FULL sync; no process/network IO
/// inside transactions. Blocking database work always runs off the Tokio executor.
pub struct Store<T> {
    connection: Arc<Mutex<rusqlite::Connection>>,
    marker: std::marker::PhantomData<T>,
    observer: Option<Arc<dyn Fn(&T) + Send + Sync>>,
}
impl<T> Clone for Store<T> {
    fn clone(&self) -> Self {
        Self {
            connection: self.connection.clone(),
            marker: std::marker::PhantomData,
            observer: self.observer.clone(),
        }
    }
}
impl<T: Default + Serialize + DeserializeOwned + Send + 'static> Store<T> {
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let c = rusqlite::Connection::open(path)?;
        c.busy_timeout(std::time::Duration::from_secs(5))?;
        c.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS control_state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);")?;
        c.execute(
            "INSERT OR IGNORE INTO control_state VALUES(1,?1)",
            [serde_json::to_string(&T::default())?],
        )?;
        Ok(Self {
            connection: Arc::new(Mutex::new(c)),
            marker: std::marker::PhantomData,
            observer: None,
        })
    }
    /// Publish an initial projection and then only committed mutations, in commit order.
    pub fn with_observer(mut self, observer: impl Fn(&T) + Send + Sync + 'static) -> Result<Self> {
        let body: String = self.connection.lock().unwrap().query_row(
            "SELECT body FROM control_state WHERE id=1",
            [],
            |r| r.get(0),
        )?;
        observer(&serde_json::from_str(&body)?);
        self.observer = Some(Arc::new(observer));
        Ok(self)
    }
    pub async fn tx<R: Send + 'static>(
        &self,
        f: impl FnOnce(&mut T) -> Result<R, ApiError> + Send + 'static,
    ) -> Result<R, ApiError> {
        let connection = self.connection.clone();
        let observer = self.observer.clone();
        tokio::task::spawn_blocking(move || {
            let mut c = connection
                .lock()
                .map_err(|_| anyhow::anyhow!("database poisoned"))?;
            let tx = c.transaction().map_err(anyhow::Error::from)?;
            let body: String = tx
                .query_row("SELECT body FROM control_state WHERE id=1", [], |r| {
                    r.get(0)
                })
                .map_err(anyhow::Error::from)?;
            let mut state: T = serde_json::from_str(&body).map_err(anyhow::Error::from)?;
            let result = f(&mut state)?;
            let next = serde_json::to_string(&state).map_err(anyhow::Error::from)?;
            let changed = next != body;
            if changed {
                tx.execute("UPDATE control_state SET body=?1 WHERE id=1", [next])
                    .map_err(anyhow::Error::from)?;
            }
            tx.commit().map_err(anyhow::Error::from)?;
            if changed && let Some(observer) = observer {
                observer(&state);
            }
            Ok(result)
        })
        .await
        .map_err(anyhow::Error::from)?
    }
}
pub fn validate_request(request: &JobRequest) -> Result<()> {
    validate("JobRequest", &serde_json::to_value(request)?)?;
    ensure!(
        serde_json::to_vec(request)?.len() <= 2 * 1024 * 1024,
        "request exceeds 2 MiB UTF-8"
    );
    ensure!(
        request
            .source
            .as_ref()
            .is_none_or(|s| s.len() <= 1024 * 1024),
        "source exceeds 1 MiB UTF-8"
    );
    let size = request
        .shape
        .iter()
        .try_fold(1i64, |n, x| n.checked_mul(*x));
    ensure!(
        size.is_some_and(|n| n <= 16_777_216),
        "total tensor elements exceed 16777216"
    );
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn contract_examples_and_unknown_fields() {
        let good: Value = serde_json::from_str(include_str!("../examples/job.json")).unwrap();
        validate("JobRequest", &good).unwrap();
        assert!(
            validate(
                "JobRequest",
                &serde_json::from_str(include_str!("../examples/invalid-job.json")).unwrap()
            )
            .is_err()
        );
        let mut unknown = good.clone();
        unknown["token"] = json!("removed");
        assert!(validate("JobRequest", &unknown).is_err());
        let dto: JobRequest = serde_json::from_value(good.clone()).unwrap();
        assert_eq!(serde_json::to_value(dto).unwrap(), good);
    }
    #[test]
    fn canonical_digest_is_order_independent() {
        assert_eq!(
            digest(&json!({"a":1,"b":{"x":2,"y":3}})),
            digest(&json!({"b":{"y":3,"x":2},"a":1}))
        );
        assert_eq!(
            digest(&json!({"small":1e-7})),
            rvbench_core::json::spec_digest(&json!({"small":1e-7}))
        );
    }
    #[test]
    fn identifiers_and_role_references_are_checked() {
        let mut ack = serde_json::json!({"worker_id":"worker","instance_id":id(),"lease_id":id(),"worker_job_id":id()});
        validate("Ack", &ack).unwrap();
        ack["instance_id"] = json!("not-a-uuid");
        assert!(validate("Ack", &ack).is_err());
        let shared: Value = serde_json::from_str(SPEC).unwrap();
        fn refs(value: &Value, shared: &Value) {
            match value {
                Value::Object(map) => {
                    if let Some(Value::String(r)) = map.get("$ref") {
                        let pointer = r
                            .strip_prefix("../../contract/openapi.json#")
                            .expect("role refs use the shared authority");
                        assert!(shared.pointer(pointer).is_some(), "unresolved {r}");
                    }
                    for v in map.values() {
                        refs(v, shared);
                    }
                }
                Value::Array(items) => {
                    for v in items {
                        refs(v, shared);
                    }
                }
                _ => {}
            }
        }
        for api in [
            include_str!("../../worker/contract/openapi.yaml"),
            include_str!("../../manager/contract/openapi.yaml"),
        ] {
            let api: Value = serde_json::from_str(api).unwrap();
            assert_eq!(api["security"], json!([]));
            assert!(api["components"]["securitySchemes"].is_null());
            refs(&api, &shared);
        }
    }
}
