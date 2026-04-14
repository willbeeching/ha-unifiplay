# Security & Code Audit Report

**Repository:** ha-unifiplay (Home Assistant custom integration for UniFi Play)  
**Date:** 2026-04-14  
**Scope:** All source code under `custom_components/unifi_play/`, CI/CD workflows, configuration files, and bundled assets

---

## Executive Summary

This is a Home Assistant custom integration written in Python that controls UniFi Play audio hardware via a REST API on the UniFi controller and direct MQTT connections to individual devices. The codebase is relatively compact (~750 lines of application code across 14 Python files) and is generally well-structured.

However, the audit identified **3 critical**, **4 high**, and **7 medium** severity findings across security and code quality.

---

## Severity Definitions

| Severity | Description |
|----------|-------------|
| **CRITICAL** | Immediate exploitation risk or credential exposure |
| **HIGH** | Significant security weakness or reliability issue |
| **MEDIUM** | Code quality or defense-in-depth concern |
| **LOW** | Minor improvement or best-practice suggestion |

---

## Security Findings

### CRITICAL-1: Private Key Committed to Repository

**File:** `custom_components/unifi_play/certs/mqtt_cert_key.key`  
**Severity:** CRITICAL

An RSA private key is committed directly to the repository in plaintext. This key is used for mTLS authentication to UniFi Play devices over MQTT. Anyone with access to this repository (which is public) can use this key to authenticate to any UniFi Play device that trusts the corresponding certificate.

```
-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEAqhYQdrFKOaJdCclWoS4ytnpliEbh4rmBrq/RjtRoLN4aCy1W
...
```

**Impact:** Any attacker on the same network as a UniFi Play device can use this key to impersonate a legitimate client, send commands, and potentially control speakers and amplifiers.

**Recommendation:** This appears to be a shared/universal client certificate that all UniFi Play clients use (extracted from the official app). If this is an intentional design choice matching how Ubiquiti's own apps work, document that fact explicitly. If it is unique to the developer, rotate the certificate immediately and load certificates from Home Assistant's storage or a user-configurable path instead.

---

### CRITICAL-2: TLS Certificate Verification Completely Disabled (HTTPS)

**File:** `custom_components/unifi_play/__init__.py:33`, `custom_components/unifi_play/api.py:47`  
**Severity:** CRITICAL

All HTTPS certificate verification is disabled for REST API communication with the UniFi controller:

```python
# __init__.py:33
session = async_get_clientsession(hass, verify_ssl=False)

# api.py:47
connector = aiohttp.TCPConnector(ssl=False)
```

**Impact:** Man-in-the-middle attacks can intercept the API key and all REST traffic between Home Assistant and the UniFi controller. An attacker on the local network could capture the API key sent in every request header.

**Recommendation:** Make SSL verification configurable in the config flow. Default to `verify_ssl=True` and allow users to disable it only if their controller uses a self-signed certificate. Consider adding a config option to provide a custom CA certificate.

---

### CRITICAL-3: TLS Certificate Verification Disabled (MQTT)

**File:** `custom_components/unifi_play/mqtt_client.py:154-160`  
**Severity:** CRITICAL

MQTT connections disable all certificate verification:

```python
self._client.tls_set(
    certfile=str(CERT_FILE),
    keyfile=str(KEY_FILE),
    cert_reqs=ssl.CERT_NONE,
    tls_version=ssl.PROTOCOL_TLS_CLIENT,
)
self._client.tls_insecure_set(True)
```

`cert_reqs=ssl.CERT_NONE` combined with `tls_insecure_set(True)` means the client will accept **any** server certificate, including one presented by an attacker performing a MITM attack.

**Impact:** An attacker could intercept and modify MQTT commands being sent to speakers (e.g., maxing volume, rebooting devices, or injecting malicious audio source changes).

**Recommendation:** Similar to CRITICAL-2, make this configurable. If the devices use self-signed certificates, allow users to provide or trust a specific CA.

---

### HIGH-1: API Key Transmitted Without Server Certificate Validation

**File:** `custom_components/unifi_play/api.py:42-43`  
**Severity:** HIGH

The API key is sent in every HTTP request header:

```python
def _headers(self) -> dict[str, str]:
    return {"X-API-KEY": self._api_key, "Accept": "application/json"}
```

Combined with CRITICAL-2 (disabled SSL verification), the API key is effectively sent in cleartext to any endpoint claiming to be the controller.

**Impact:** API key theft enables full control over the UniFi Play ecosystem via the REST API.

**Recommendation:** This is automatically mitigated by fixing CRITICAL-2. Additionally, consider caching the API key in memory only (not logging it) and ensuring it doesn't appear in error messages or stack traces.

---

### HIGH-2: No Input Validation on Controller Host

**File:** `custom_components/unifi_play/config_flow.py:16-21`  
**Severity:** HIGH

The config flow accepts any string as the controller host with no validation:

```python
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONTROLLER_HOST): str,
        vol.Required(CONF_API_KEY): str,
    }
)
```

**Impact:** While Home Assistant's config flow provides some protection, a malformed or malicious hostname could lead to unexpected behavior. More importantly, there's no SSRF protection — the integration will make HTTP requests to whatever host is specified.

**Recommendation:** Add hostname/IP validation using `vol.Match` or a custom validator. Consider restricting to RFC-compliant hostnames and private IP ranges.

---

### HIGH-3: Failed MQTT Client Registered but Never Retried

**File:** `custom_components/unifi_play/coordinator.py:172-179`  
**Severity:** HIGH

When an MQTT connection fails, the client object is still stored in `_mqtt_clients`, preventing any future retry:

```python
client = UnifiPlayMqttClient(ip, mac, on_event=_schedule_event)
self._mqtt_clients[device_id] = client  # stored BEFORE connect attempt
try:
    await client.connect()
    await asyncio.sleep(0.5)
    client.request_info()
except Exception:
    _LOGGER.exception("Failed to connect MQTT to %s (%s)", ip, mac)
    # client stays in _mqtt_clients — never retried
```

**Impact:** If a device is temporarily offline during initial setup, it will never get an MQTT connection established until the entire integration is reloaded. This is a reliability/availability issue.

**Recommendation:** Remove the client from `_mqtt_clients` in the `except` block, or implement a retry mechanism with backoff.

---

### HIGH-4: Stale Devices Never Pruned

**File:** `custom_components/unifi_play/coordinator.py:154-162`  
**Severity:** HIGH

The `_async_update_data` method only adds new devices — it never removes devices that have disappeared from the REST API:

```python
for dev in devices:
    dev_id = dev["id"]
    if dev_id not in self._device_states:
        self._device_states[dev_id] = UnifiPlayDeviceState(dev)
    # ...
return self._device_states
```

**Impact:** MQTT connections to removed/offline devices persist indefinitely, consuming resources. Device states become stale and could show incorrect information.

**Recommendation:** Track which device IDs were returned by the REST API and disconnect/remove MQTT clients and state objects for devices no longer present.

---

## Code Quality Findings

### MEDIUM-1: Synchronous MQTT Publish Called from Async Context

**Files:** `media_player.py`, `switch.py`, `number.py`, `select.py`, `button.py`, `text.py`  
**Severity:** MEDIUM

All entity command methods are `async` but call synchronous paho-mqtt `publish()` through the MQTT client:

```python
# media_player.py:105-108
async def async_set_volume_level(self, volume: float) -> None:
    client = self._mqtt()
    if client:
        client.set_volume(int(volume * 100))  # calls self._client.publish() synchronously
```

**Impact:** If the paho-mqtt `publish()` call blocks (network congestion, buffer full), it will block the Home Assistant event loop, causing UI freezes and delayed automations.

**Recommendation:** Wrap synchronous MQTT publish calls in `asyncio.get_event_loop().run_in_executor()` or use an async MQTT library.

---

### MEDIUM-2: Unchecked Dictionary Access in Entity Base

**File:** `custom_components/unifi_play/entity.py:36-37`  
**Severity:** MEDIUM

```python
@property
def _device_state(self) -> UnifiPlayDeviceState:
    return self.coordinator.data[self._device_id]
```

**Impact:** If coordinator data is refreshed and a device is removed (or during a race condition), this raises `KeyError`, potentially causing entity failures.

**Recommendation:** Use `.get()` with a fallback, or handle `KeyError` gracefully with `entity_available = False`.

---

### MEDIUM-3: Broad Exception Handling

**Files:** `mqtt_client.py:141`, `coordinator.py:178`, `config_flow.py:50`  
**Severity:** MEDIUM

Multiple locations catch `except Exception` broadly:

```python
except Exception:
    _LOGGER.exception("Error parsing MQTT message from %s", self._device_ip)
```

**Impact:** This swallows unexpected errors (like `MemoryError`, `SystemExit`, or programming bugs) making debugging harder and potentially masking serious issues.

**Recommendation:** Catch more specific exceptions. At minimum, avoid catching `BaseException` subclasses by using `except Exception` (which is done) but consider narrowing further.

---

### MEDIUM-4: Response Body Leaked in Error Messages

**File:** `custom_components/unifi_play/api.py:61-64`  
**Severity:** MEDIUM

```python
text = await resp.text()
raise UnifiPlayApiError(
    f"Unexpected response ({resp.status}): {text[:200]}"
)
```

**Impact:** Error messages containing up to 200 characters of the response body may appear in Home Assistant logs, potentially leaking sensitive information from the controller.

**Recommendation:** Limit to status code and content type in the error message. Log the full response body only at `DEBUG` level.

---

### MEDIUM-5: Dead Code — `get_groups()` Method

**File:** `custom_components/unifi_play/api.py:80-83`  
**Severity:** MEDIUM

```python
async def get_groups(self) -> list[dict]:
    """Return a list of speaker groups."""
    data = await self._request("GET", "/groups")
    return data.get("data") or []
```

This method is never called anywhere in the codebase.

**Recommendation:** Remove unused code or implement the speaker groups feature. Dead code increases maintenance burden and review surface.

---

### MEDIUM-6: No `update_interval` Set on Coordinator

**File:** `custom_components/unifi_play/coordinator.py:137-142`  
**Severity:** MEDIUM

```python
super().__init__(
    hass,
    _LOGGER,
    name="UniFi Play",
)
```

The coordinator is initialized without an `update_interval`, meaning `_async_update_data` is only called once (during first refresh). There's a `DEFAULT_SCAN_INTERVAL = 30` constant defined in `const.py` but it's never used.

**Impact:** New devices added to the controller after initial setup will never be discovered. If the REST API returns updated device metadata, those changes won't be reflected.

**Recommendation:** Pass `update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL)` to the coordinator constructor.

---

### MEDIUM-7: MQTT Event Floods Trigger Excessive State Writes

**File:** `custom_components/unifi_play/coordinator.py:199`  
**Severity:** MEDIUM

```python
self.async_set_updated_data(self._device_states)
```

Every single MQTT event triggers a full coordinator data update, which fans out to all entities for all devices.

**Impact:** High-frequency MQTT events (e.g., rapid volume changes, streaming metadata updates) can cause excessive state writes and entity updates, impacting Home Assistant performance.

**Recommendation:** Implement debouncing (e.g., `async_call_later` with a short delay) or use targeted entity updates instead of full coordinator refreshes.

---

## Low Severity / Informational

### LOW-1: CI Pipeline Lacks Security Scanning

The CI pipeline (`ci.yaml`) runs formatting checks (Black, isort, flake8) and HACS/Hassfest validation, but has no security scanning (e.g., Bandit for Python security linting, dependency vulnerability scanning).

**Recommendation:** Add `bandit` to the CI pipeline for Python security linting. Consider adding `safety` or `pip-audit` for dependency vulnerability scanning.

---

### LOW-2: `getattr` Used for Dynamic Method Dispatch

**Files:** `switch.py:91,96`, `number.py:146`, `select.py:119`, `button.py:74`

```python
getattr(client, self.entity_description.set_fn)(True)
```

While the method names come from trusted internal dataclass definitions (not user input), `getattr` bypasses static analysis and IDE tooling.

**Recommendation:** Consider using a callback/callable pattern instead of string-based method dispatch for better type safety.

---

### LOW-3: No Tests

The repository contains no test files (`test_*.py`, `*_test.py`, `conftest.py`, etc.).

**Recommendation:** Add unit tests, especially for `decode_binme`/`encode_binme` (binary protocol parsing), API error handling, and coordinator state management. These are the most critical paths for correctness and security.

---

## CI/CD Security Review

| Item | Status | Notes |
|------|--------|-------|
| GitHub Actions pinned to tags | Partial | `actions/checkout@v6` and `actions/setup-python@v6` use major version tags (acceptable). `hacs/action@main` and `home-assistant/actions/hassfest@master` track branch heads (riskier). |
| Secrets handling | OK | Only `GITHUB_TOKEN` used, with appropriate `permissions: contents: write` scope. |
| Release workflow | OK | Tag-triggered, generates changelog from git log. |
| Dependency pinning | Partial | `paho-mqtt>=2.0.0` uses minimum version, not pinned. CI installs `flake8 black isort` without version pins. |

**Recommendation:** Pin third-party GitHub Actions to full SHA hashes. Pin CI tool versions for reproducible builds.

---

## Summary of Recommendations (Priority Order)

1. **Make TLS verification configurable** with secure defaults for both HTTPS and MQTT (CRITICAL-2, CRITICAL-3)
2. **Document the shared certificate situation** or move certificates out of the repository (CRITICAL-1)
3. **Add hostname validation** in the config flow (HIGH-2)
4. **Fix MQTT client lifecycle** — remove failed clients, prune stale devices, implement retry (HIGH-3, HIGH-4)
5. **Set coordinator `update_interval`** to enable periodic device discovery (MEDIUM-6)
6. **Wrap synchronous MQTT calls** in executors to avoid blocking the event loop (MEDIUM-1)
7. **Add security scanning** (Bandit) to CI pipeline (LOW-1)
8. **Add unit tests** for critical code paths (LOW-3)
9. **Remove dead code** (`get_groups`) (MEDIUM-5)
10. **Implement event debouncing** for MQTT state updates (MEDIUM-7)
