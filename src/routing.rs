use serde_json::{json, Value};
use std::time::{Duration, Instant};

pub fn retry_delay(failure: &Value, elapsed: Duration, attempt: u32) -> Option<Duration> {
    // Only an explicit, bounded temporary refusal authorizes another request.
    // Authentication, policy, pricing, daily quotas, hosting budgets, malformed
    // streams and ambiguous transport failures must never become retry loops.
    if attempt >= 7
        || !matches!(
            failure["code"].as_str(),
            Some(
                "free_capacity_exhausted"
                    | "provider_rate_limited"
                    | "upstream_rate_limited"
                    | "provider_timeout"
                    | "provider_unavailable"
                    | "recovery_exhausted"
                    | "capacity_busy"
                    | "client_rate_limited"
            )
        )
    {
        return None;
    }
    let seconds = failure["retry_after_seconds"].as_u64()?;
    if seconds == 0 || seconds > 120 {
        return None;
    }
    let delay = Duration::from_secs(seconds.max((5u64 << attempt).min(60)));
    (elapsed + delay + Duration::from_secs(15) < Duration::from_secs(300)).then_some(delay)
}

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

    pub fn retry(&mut self) {
        self.failed.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn waits_for_temporary_capacity_without_retrying_policy_or_daily_exhaustion() {
        let busy = json!({"code":"free_capacity_exhausted", "retry_after_seconds":57});
        assert_eq!(
            retry_delay(&busy, Duration::ZERO, 0),
            Some(Duration::from_secs(57))
        );
        assert_eq!(retry_delay(&busy, Duration::from_secs(250), 0), None);
        assert_eq!(retry_delay(&busy, Duration::ZERO, 7), None);
        for code in [
            "budget_exhausted",
            "upstream_quota",
            "upstream_policy",
            "upstream_authentication",
            "pricing_unavailable",
        ] {
            assert_eq!(
                retry_delay(
                    &json!({"code":code, "retry_after_seconds":1}),
                    Duration::ZERO,
                    0
                ),
                None
            );
        }
        for seconds in [json!(0), json!(3600), json!(-1), json!(1.5), json!("1")] {
            assert_eq!(
                retry_delay(
                    &json!({"code":"free_capacity_exhausted", "retry_after_seconds":seconds}),
                    Duration::ZERO,
                    0
                ),
                None
            );
        }
    }

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
