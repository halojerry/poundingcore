use std::time::Duration;

use aionui_api_types::ModelInfo;
use serde::Deserialize;
use tracing::warn;

use crate::error::SystemError;

use super::FetchConfig;

const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);

/// Dispatch to the appropriate platform-specific fetcher.
pub(crate) async fn fetch_for_platform(
    client: &reqwest::Client,
    config: &FetchConfig,
) -> Result<Vec<ModelInfo>, SystemError> {
    match config.platform.as_str() {
        "anthropic" | "claude" => fetch_anthropic(client, &config.base_url, &config.api_key).await,
        "gemini" => fetch_gemini(client, &config.base_url, &config.api_key).await,
        "bedrock" => fetch_bedrock(config).await,
        "vertex-ai" => Ok(vertex_ai_models()),
        "new-api" => fetch_new_api(client, &config.base_url, &config.api_key).await,
        "minimax" => Ok(minimax_models()),
        "dashscope-coding" => fetch_dashscope_coding(client, &config.base_url, &config.api_key).await,
        _ => fetch_openai_compatible(client, &config.base_url, &config.api_key).await,
    }
}

// ---------------------------------------------------------------------------
// OpenAI-compatible (default)
// ---------------------------------------------------------------------------

/// Response shape for OpenAI `/models` endpoint.
#[derive(Deserialize)]
struct OpenAiModelsResponse {
    data: Vec<OpenAiModel>,
}

#[derive(Deserialize)]
struct OpenAiModel {
    id: String,
}

/// Fetch models from an OpenAI-compatible `/models` endpoint.
pub(super) async fn fetch_openai_compatible(
    client: &reqwest::Client,
    base_url: &str,
    api_key: &str,
) -> Result<Vec<ModelInfo>, SystemError> {
    let url = format!("{}/models", base_url.trim_end_matches('/'));
    let resp = client
        .get(&url)
        .header("Authorization", format!("Bearer {api_key}"))
        .timeout(REQUEST_TIMEOUT)
        .send()
        .await
        .map_err(|e| remote_error(&e))?;

    check_response_status(&resp)?;

    let body: OpenAiModelsResponse = resp
        .json()
        .await
        .map_err(|e| SystemError::BadGateway(format!("Failed to parse models response: {e}")))?;

    Ok(body.data.into_iter().map(|m| ModelInfo::Id(m.id)).collect())
}

// ---------------------------------------------------------------------------
// Anthropic
// ---------------------------------------------------------------------------

/// Response shape for Anthropic `/v1/models`.
#[derive(Deserialize)]
struct AnthropicModelsResponse {
    data: Vec<AnthropicModel>,
}

#[derive(Deserialize)]
struct AnthropicModel {
    id: String,
}

const ANTHROPIC_FALLBACK_MODELS: &[&str] = &[
    "claude-sonnet-4-20250514",
    "claude-opus-4-20250514",
    "claude-3-7-sonnet-20250219",
];

async fn fetch_anthropic(
    client: &reqwest::Client,
    base_url: &str,
    api_key: &str,
) -> Result<Vec<ModelInfo>, SystemError> {
    let url = format!("{}/v1/models", base_url.trim_end_matches('/'));
    let result = client
        .get(&url)
        .header("x-api-key", api_key)
        .header("anthropic-version", "2023-06-01")
        .timeout(REQUEST_TIMEOUT)
        .send()
        .await;

    match result {
        Ok(resp) if resp.status().is_success() => {
            let body: AnthropicModelsResponse = resp
                .json()
                .await
                .map_err(|e| SystemError::BadGateway(format!("Failed to parse Anthropic response: {e}")))?;
            Ok(body.data.into_iter().map(|m| ModelInfo::Id(m.id)).collect())
        }
        Ok(resp) => {
            warn!(
                status = %resp.status(),
                "Anthropic models API failed, using fallback list"
            );
            Ok(fallback_models(ANTHROPIC_FALLBACK_MODELS))
        }
        Err(e) => {
            warn!(error = %e, "Anthropic models API unreachable, using fallback list");
            Ok(fallback_models(ANTHROPIC_FALLBACK_MODELS))
        }
    }
}

// ---------------------------------------------------------------------------
// Gemini
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct GeminiModelsResponse {
    models: Vec<GeminiModel>,
}

#[derive(Deserialize)]
struct GeminiModel {
    name: String,
}

const GEMINI_FALLBACK_MODELS: &[&str] = &["gemini-2.5-pro", "gemini-2.5-flash"];

async fn fetch_gemini(client: &reqwest::Client, base_url: &str, api_key: &str) -> Result<Vec<ModelInfo>, SystemError> {
    let url = format!("{}/v1beta/models?key={api_key}", base_url.trim_end_matches('/'));
    let result = client.get(&url).timeout(REQUEST_TIMEOUT).send().await;

    match result {
        Ok(resp) if resp.status().is_success() => {
            let body: GeminiModelsResponse = resp
                .json()
                .await
                .map_err(|e| SystemError::BadGateway(format!("Failed to parse Gemini response: {e}")))?;
            let models = body
                .models
                .into_iter()
                .map(|m| {
                    // Strip "models/" prefix: "models/gemini-2.5-pro" -> "gemini-2.5-pro"
                    let id = m.name.strip_prefix("models/").unwrap_or(&m.name).to_owned();
                    ModelInfo::Id(id)
                })
                .collect();
            Ok(models)
        }
        Ok(resp) => {
            warn!(
                status = %resp.status(),
                "Gemini models API failed, using fallback list"
            );
            Ok(fallback_models(GEMINI_FALLBACK_MODELS))
        }
        Err(e) => {
            warn!(error = %e, "Gemini models API unreachable, using fallback list");
            Ok(fallback_models(GEMINI_FALLBACK_MODELS))
        }
    }
}

// ---------------------------------------------------------------------------
// Bedrock (AWS SDK)
// ---------------------------------------------------------------------------

async fn fetch_bedrock(config: &FetchConfig) -> Result<Vec<ModelInfo>, SystemError> {
    let bedrock_cfg = config
        .bedrock_config
        .as_ref()
        .ok_or_else(|| SystemError::BadRequest("Bedrock requires bedrockConfig".into()))?;

    let region = aws_sdk_bedrock::config::Region::new(bedrock_cfg.region.clone());

    let sdk_config = match bedrock_cfg.auth_method {
        aionui_api_types::BedrockAuthMethod::AccessKey => {
            let key_id = bedrock_cfg
                .access_key_id
                .as_deref()
                .ok_or_else(|| SystemError::BadRequest("accessKeyId is required".into()))?;
            let secret = bedrock_cfg
                .secret_access_key
                .as_deref()
                .ok_or_else(|| SystemError::BadRequest("secretAccessKey is required".into()))?;

            let creds = aws_sdk_bedrock::config::Credentials::new(
                key_id, secret, None, // session token
                None, // expiry
                "aionui",
            );
            aws_sdk_bedrock::Config::builder()
                .region(region)
                .credentials_provider(creds)
                .build()
        }
        aionui_api_types::BedrockAuthMethod::Profile => {
            let profile = bedrock_cfg.profile.as_deref().unwrap_or("default");
            let aws_cfg = aws_config::from_env()
                .profile_name(profile)
                .region(aws_config::Region::new(bedrock_cfg.region.clone()))
                .load()
                .await;
            aws_sdk_bedrock::Config::new(&aws_cfg)
        }
    };

    let client = aws_sdk_bedrock::Client::from_conf(sdk_config);
    let resp = client
        .list_inference_profiles()
        .send()
        .await
        .map_err(|e| SystemError::BadGateway(format!("Bedrock API error: {e}")))?;

    let profiles = resp.inference_profile_summaries();
    // Filter to only anthropic.claude models per API Spec
    let models: Vec<ModelInfo> = profiles
        .iter()
        .filter(|p| p.inference_profile_id().starts_with("anthropic.claude"))
        .map(|p| ModelInfo::Id(p.inference_profile_id().to_string()))
        .collect();

    Ok(models)
}

// ---------------------------------------------------------------------------
// Hardcoded platforms
// ---------------------------------------------------------------------------

fn vertex_ai_models() -> Vec<ModelInfo> {
    vec![
        ModelInfo::Id("gemini-2.5-pro".into()),
        ModelInfo::Id("gemini-2.5-flash".into()),
    ]
}

fn minimax_models() -> Vec<ModelInfo> {
    vec![
        ModelInfo::Id("MiniMax-Text-01".into()),
        ModelInfo::Id("abab6.5s-chat".into()),
        ModelInfo::Id("abab6.5-chat".into()),
    ]
}

// ---------------------------------------------------------------------------
// new-api (OpenAI-compatible with /v1 enforcement)
// ---------------------------------------------------------------------------
// POUNDING new-api (with group filtering)
// ---------------------------------------------------------------------------

/// Fetch models from the POUNDING API `/api/user/models?group=POUNDING`.
///
/// The POUNDING API returns a `NewApiResponse` wrapper:
/// `{ "data": [ "model-id-1", "model-id-2", ... ] }`
/// or `{ "data": { "data": [ "model-id-1", ... ] } }`
/// Each element may be a flat string or `{ "id": "...", "owned_by": "..." }`.
async fn fetch_models_at_url(
    client: &reqwest::Client,
    url: &str,
    api_key: &str,
) -> Result<Vec<ModelInfo>, SystemError> {
    let resp = client
        .get(url)
        .header("Authorization", format!("Bearer {api_key}"))
        .timeout(REQUEST_TIMEOUT)
        .send()
        .await
        .map_err(|e| remote_error(&e))?;

    check_response_status(&resp)?;

    // Read the raw body as text first so we can retry parsing without
    // re-fetching (resp.json() consumes the body).
    let text = resp.text().await.map_err(|e| remote_error(&e))?;
    let body: serde_json::Value = serde_json::from_str(&text)
        .map_err(|e| SystemError::BadGateway(format!("Failed to parse POUNDING API response: {e}")))?;

    // Normalize: extract the flat model array, matching the same field order
    // as the frontend's normalizeModelList().
    let data = body.get("data");
    let models_arr = data
        .and_then(|d| {
            d.get("data") // { data: { data: [...] } }
                .or_else(|| d.get("models")) // { data: { models: [...] } }
                .or_else(|| if d.is_array() { Some(d) } else { None }) // { data: [...] }
        })
        .or_else(|| body.get("data").filter(|d| d.is_array())) // { data: [...] }
        .and_then(|v| v.as_array());

    let items: Vec<String> = match models_arr {
        Some(arr) => arr
            .iter()
            .filter_map(|item| {
                if let Some(s) = item.as_str() {
                    return Some(s.to_string());
                }
                if let Some(obj) = item.as_object() {
                    obj.get("id")
                        .or_else(|| obj.get("model_name"))
                        .or_else(|| obj.get("model"))
                        .and_then(|v| v.as_str())
                        .map(String::from)
                } else {
                    None
                }
            })
            .filter(|s| !s.is_empty())
            .collect(),
        None => Vec::new(),
    };

    Ok(items.into_iter().map(ModelInfo::Id).collect())
}

async fn fetch_new_api(client: &reqwest::Client, base_url: &str, api_key: &str) -> Result<Vec<ModelInfo>, SystemError> {
    // POUNDING: only fetch models from the POUNDING group.
    // The mxou API's /api/user/models?group=POUNDING returns a flat array
    // of models whose abilities row has group='POUNDING' AND enabled=true.
    let trimmed = base_url.trim_end_matches('/');
    let url = format!("{trimmed}/api/user/models?group=POUNDING");
    fetch_models_at_url(client, &url, api_key).await
}

// ---------------------------------------------------------------------------
// dashscope-coding (hardcoded + key validation)
// ---------------------------------------------------------------------------

const DASHSCOPE_MODELS: &[&str] = &["qwen-coder-plus", "qwen-coder-turbo"];

async fn fetch_dashscope_coding(
    client: &reqwest::Client,
    base_url: &str,
    api_key: &str,
) -> Result<Vec<ModelInfo>, SystemError> {
    // Validate key by sending a minimal chat completion request
    let url = format!("{}/chat/completions", base_url.trim_end_matches('/'));
    let body = serde_json::json!({
        "model": DASHSCOPE_MODELS[0],
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1
    });

    let resp = client
        .post(&url)
        .header("Authorization", format!("Bearer {api_key}"))
        .json(&body)
        .timeout(REQUEST_TIMEOUT)
        .send()
        .await
        .map_err(|e| remote_error(&e))?;

    if resp.status().is_client_error() {
        return Err(SystemError::BadGateway(format!(
            "Dashscope API key validation failed: {}",
            resp.status()
        )));
    }

    Ok(fallback_models(DASHSCOPE_MODELS))
}

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

fn fallback_models(ids: &[&str]) -> Vec<ModelInfo> {
    ids.iter().map(|id| ModelInfo::Id((*id).to_string())).collect()
}

fn check_response_status(resp: &reqwest::Response) -> Result<(), SystemError> {
    if resp.status().is_success() {
        return Ok(());
    }
    Err(SystemError::BadGateway(format!(
        "Remote API returned {}",
        resp.status()
    )))
}

fn remote_error(e: &reqwest::Error) -> SystemError {
    if e.is_timeout() {
        SystemError::Timeout("Remote API request timed out".into())
    } else {
        SystemError::BadGateway(format!("Remote API request failed: {e}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn vertex_ai_returns_expected_models() {
        let models = vertex_ai_models();
        assert_eq!(models.len(), 2);
        assert_eq!(models[0], ModelInfo::Id("gemini-2.5-pro".into()));
        assert_eq!(models[1], ModelInfo::Id("gemini-2.5-flash".into()));
    }

    #[test]
    fn minimax_returns_expected_models() {
        let models = minimax_models();
        assert_eq!(models.len(), 3);
        assert_eq!(models[0], ModelInfo::Id("MiniMax-Text-01".into()));
    }

    #[test]
    fn fallback_models_builds_model_info_list() {
        let models = fallback_models(&["a", "b", "c"]);
        assert_eq!(models.len(), 3);
        assert_eq!(models[0], ModelInfo::Id("a".into()));
    }
}
