use serde_json::{json, Value};
use std::time::{Duration, Instant};

// Process-local session hints, never written to disk or used as identity.
#[derive(Default)]
pub struct Routing {
    preferred: Option<String>,
    failed: Vec<(String, Instant)>,
}

impl Routing {
    pub fn hints(&mut self, body: &mut Value) {
        self.failed.retain(|(_, until)| *until > Instant::now());
        if body["model"] != "auto" {
            return;
        }
        if let Some(model) = &self.preferred {
            body["preferred_model"] = json!(model);
        }
        body["avoid_models"] = json!(self.failed.iter().map(|(m, _)| m).collect::<Vec<_>>());
    }

    pub fn observe(&mut self, event: &Value) {
        let seconds = event["cooldown_seconds"]
            .as_u64()
            .unwrap_or(300)
            .clamp(300, 86400);
        if let Some(models) = event["failed_models"].as_array() {
            for model in models.iter().filter_map(Value::as_str).take(35) {
                if !model.ends_with(":free") || model.len() > 256 {
                    continue;
                }
                self.failed.retain(|(m, _)| m != model);
                self.failed
                    .push((model.into(), Instant::now() + Duration::from_secs(seconds)));
                if self.failed.len() > 35 {
                    self.failed.remove(0);
                }
            }
        }
    }

    pub fn success(&mut self, model: &str) {
        self.preferred = Some(model.into());
        self.failed.retain(|(m, _)| m != model);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn session_keeps_success_avoids_failures_and_expires_them() {
        let mut routing = Routing::default();
        routing.observe(&json!({"failed_models":["test/a:free"]}));
        routing.success("test/b:free");
        let mut body = json!({"model":"auto"});
        routing.hints(&mut body);
        assert_eq!(body["preferred_model"], "test/b:free");
        assert_eq!(body["avoid_models"], json!(["test/a:free"]));
        routing.failed[0].1 = Instant::now() - Duration::from_secs(1);
        routing.hints(&mut body);
        assert_eq!(body["avoid_models"], json!([]));
        let mut pinned = json!({"model":"test/a:free"});
        routing.hints(&mut pinned);
        assert_eq!(pinned, json!({"model":"test/a:free"}));
    }
}
