#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn forged_readiness_and_non_loopback_are_refused() {
        let token = "a".repeat(64);
        assert!(verified_origin(r#"{"origin":"http://127.0.0.1:12345","proof":"forged"}"#, &token).is_err());
        assert!(verified_origin(r#"{"origin":"http://example.com:12345"}"#, &token).is_err());
        assert!(verified_origin("not json", &token).is_err());
        assert!(verified_origin(&format!("{{\"origin\":\"http://127.0.0.1:12345\",\"proof\":\"{}\"}}", "é".repeat(32)), &token).is_err());
    }

    #[test]
    fn known_companion_proof_is_accepted() {
        // Independently calculated with Python hmac.digest, not this verifier.
        let line = r#"{"origin":"http://127.0.0.1:12345","proof":"7fa6057f150c55ec15b661cf1666e95023f0c9dd0f61b0f356ffb72bb5de9992"}"#;
        assert_eq!(verified_origin(line, &"a".repeat(64)).unwrap().port(), Some(12345));
    }
}

pub fn verified_origin(line: &str, token: &str) -> Result<url::Url, Box<dyn std::error::Error>> {
    use hmac::{Hmac, KeyInit, Mac};
    let ready: serde_json::Value = serde_json::from_str(line)?;
    let origin = ready["origin"].as_str().ok_or("Missing main origin")?;
    let proof = ready["proof"].as_str().ok_or("Missing readiness proof")?;
    if proof.len() != 64 || !proof.bytes().all(|b| b.is_ascii_hexdigit()) { return Err("Invalid readiness proof".into()); }
    let bytes: Result<Vec<u8>, _> = (0..64).step_by(2).map(|i| u8::from_str_radix(&proof[i..i+2], 16)).collect();
    let mut mac = Hmac::<sha2::Sha256>::new_from_slice(token.as_bytes())?;
    mac.update(origin.as_bytes());
    mac.verify_slice(&bytes?)?;
    let url: url::Url = origin.parse()?;
    if url.scheme() != "http" || url.host_str() != Some("127.0.0.1") || url.port().is_none()
        || !url.username().is_empty() || url.password().is_some() || url.path() != "/"
        || url.query().is_some() || url.fragment().is_some() {
        return Err("Invalid main readiness origin".into());
    }
    Ok(url)
}
