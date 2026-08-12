"""Safe Exam Browser (SEB) server-side integration for the MESA POC.

What this module does (all per the official SEB developer specs):
  1. Generates a .seb configuration file SEB can open: an XML plist,
     prefixed with the 4-byte marker "plnd" (plain/unencrypted), gzip-wrapped.
  2. Verifies the headers SEB attaches to every request when
     `sendBrowserExamKey` is enabled in the config:
       X-SafeExamBrowser-RequestHash   = SHA256(absoluteURL + BrowserExamKey)
       X-SafeExamBrowser-ConfigKeyHash = SHA256(absoluteURL + ConfigKey)
     The keys themselves are displayed by the SEB Config Tool; the admin
     pastes them into .env (same model Moodle uses).

What it does NOT do: it cannot make a normal browser behave like SEB, and it
cannot compute the Browser Exam Key server-side (that key incorporates the
SEB executable itself, by design). Without keys pasted into .env, enforcement
falls back to a User-Agent check, which is WEAK and spoofable — fine for
seeing the flow work, not evidence of lockdown.
"""
import gzip
import hashlib
import plistlib


def build_seb_config(start_url: str, quit_url: str = "",
                     quit_password: str = "") -> bytes:
    """Return .seb file bytes: gzip( b'plnd' + XML-plist )."""
    settings = {
        "sebConfigPurpose": 0,          # 0 = "starting an exam"
        "startURL": start_url,
        "sendBrowserExamKey": True,     # makes SEB send the verification headers
        "allowQuit": True,
        "browserWindowAllowReload": True,
    }
    if quit_url:
        settings["quitURL"] = quit_url
    if quit_password:
        # SEB stores hashed passwords as uppercase Base16 SHA256
        settings["hashedQuitPassword"] = (
            hashlib.sha256(quit_password.encode("utf-8")).hexdigest().upper())
    xml = plistlib.dumps(settings, fmt=plistlib.FMT_XML)
    return gzip.compress(b"plnd" + xml)


def parse_seb_config(data: bytes) -> dict:
    """Inverse of build_seb_config (used by tests)."""
    inner = gzip.decompress(data)
    if inner[:4] != b"plnd":
        raise ValueError("not a plain (plnd) .seb container")
    return plistlib.loads(inner[4:])


def strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


def expected_hash(url: str, key: str) -> str:
    """SHA256(absoluteURL-without-fragment + key), lowercase Base16."""
    return hashlib.sha256(
        (strip_fragment(url) + key).encode("utf-8")).hexdigest()


def check_hash(url: str, key: str, received=None) -> bool:
    if not received:
        return False
    return expected_hash(url, key) == received.strip().lower()


def verify_request(url: str, headers, bek: str, ck: str):
    """Decide whether a request came from SEB.

    Returns (verified: bool, method: str). Preference order:
      Browser Exam Key (strongest — ties to SEB binary + config)
      Config Key       (ties to the exact config file)
      User-Agent       (weak fallback, spoofable — flagged as such)
    """
    if bek:
        return (check_hash(url, bek,
                           headers.get("X-SafeExamBrowser-RequestHash")),
                "browser-exam-key")
    if ck:
        return (check_hash(url, ck,
                           headers.get("X-SafeExamBrowser-ConfigKeyHash")),
                "config-key")
    ua = headers.get("User-Agent", "") or ""
    return ("SEB" in ua, "user-agent (WEAK, spoofable — paste keys into .env)")
