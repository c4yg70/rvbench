use anyhow::{Result, ensure};
use reqwest::{Method, blocking::Client as HttpClient};
use serde_json::Value;
use std::{
    thread,
    time::{Duration, Instant},
};

pub struct Client {
    pub base: String,
    token: Option<String>,
    http: HttpClient,
}
impl Client {
    pub fn new(base: &str, token: Option<String>) -> Result<Self> {
        Ok(Self {
            base: base.trim_end_matches('/').into(),
            token,
            http: HttpClient::builder()
                .timeout(Duration::from_secs(90))
                .build()?,
        })
    }
    pub fn request(&self, method: Method, path: &str, body: Option<&Value>) -> Result<Value> {
        self.request_with_timeout(method, path, body, Duration::from_secs(90))
    }
    fn request_with_timeout(
        &self,
        method: Method,
        path: &str,
        body: Option<&Value>,
        timeout: Duration,
    ) -> Result<Value> {
        let (status, result) = self.response(method, path, body, timeout)?;
        ensure!(
            status.is_success(),
            "evald HTTP {}: {}",
            status.as_u16(),
            result
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("request failed")
        );
        Ok(result)
    }
    fn response(
        &self,
        method: Method,
        path: &str,
        body: Option<&Value>,
        timeout: Duration,
    ) -> Result<(reqwest::StatusCode, Value)> {
        let mut request = self
            .http
            .request(method, format!("{}{path}", self.base))
            .timeout(timeout);
        if let Some(token) = &self.token {
            request = request.header("X-K3-Token", token);
        }
        if let Some(body) = body {
            request = request.json(body);
        }
        let response = request.send()?;
        let status = response.status();
        let text = response.text()?;
        let value = rvbench_core::parse_json(&text)
            .map_err(|_| anyhow::anyhow!("evald returned invalid JSON"))?;
        Ok((status, value))
    }
    pub fn submit(&self, body: &Value, budget: Duration) -> Result<Value> {
        let deadline = Instant::now() + budget;
        let backoff = [30, 60, 120, 240];
        let mut retries = 0;
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            ensure!(!remaining.is_zero(), "queue admission budget exhausted");
            let (status, result) = self.response(
                Method::POST,
                "/jobs",
                Some(body),
                remaining.min(Duration::from_secs(90)),
            )?;
            if status.as_u16() != 429 {
                ensure!(
                    status.is_success(),
                    "evald HTTP {}: {}",
                    status.as_u16(),
                    result["error"]
                );
                return Ok(result);
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            ensure!(!remaining.is_zero(), "queue admission budget exhausted");
            thread::sleep(Duration::from_secs(backoff[retries.min(3)]).min(remaining));
            retries += 1;
        }
    }
    pub fn wait(&self, id: &str, budget: Duration) -> Result<Value> {
        ensure!(uuid_is_safe(id), "invalid job identifier");
        let deadline = Instant::now() + budget;
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            ensure!(
                !remaining.is_zero(),
                "client wait budget exhausted; query job {id} to resume"
            );
            let wait = remaining.as_secs().clamp(1, 60);
            let result = self.request_with_timeout(
                Method::GET,
                &format!("/jobs/{id}?wait={wait}"),
                None,
                remaining.min(Duration::from_secs(90)),
            )?;
            if ["done", "failed", "timeout", "canceled"]
                .contains(&result["state"].as_str().unwrap_or(""))
            {
                return Ok(result);
            }
        }
    }
}
pub fn uuid_is_safe(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'-')
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        io::{BufRead, BufReader, Read, Write},
        net::TcpListener,
    };
    #[test]
    fn full_queue_retry_stops_at_admission_budget() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut length = 0;
            loop {
                let mut line = String::new();
                assert!(reader.read_line(&mut line).unwrap() > 0);
                if line == "\r\n" {
                    break;
                }
                if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                    length = value.trim().parse::<usize>().unwrap();
                }
            }
            let mut body = vec![0u8; length];
            reader.read_exact(&mut body).unwrap();
            stream.write_all(b"HTTP/1.1 429 Too Many Requests\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}").unwrap();
        });
        let client = Client::new(&format!("http://{address}"), None).unwrap();
        let error = client
            .submit(&serde_json::json!({"op":"abs"}), Duration::from_millis(200))
            .unwrap_err();
        // A further request would connect to the closed listener and report a transport error.
        assert!(
            error.to_string().contains("admission budget exhausted"),
            "{error}"
        );
        server.join().unwrap();
    }
}
