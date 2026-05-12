"""Tests for pinky_identity.signer (DaemonSigner)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

import pytest
import requests

from pinky_identity import (
    DEFAULT_TAG,
    DEVICE_KEY_BYTES,
    KEY_ACTIVE,
    PURPOSE_SERVICE_AUTH,
    DaemonSigner,
    DeviceKey,
    EncryptedSignerStore,
    KeyNotActiveError,
    KeyNotInStoreError,
    KeyNotRegisteredError,
    KidFingerprintMismatchError,
    PinkyKeyResolver,
    SignatureError,
    SignerError,
    fingerprint,
    generate_keypair,
    kid_from_public_key,
    verify_request,
)
from pinky_identity.signer import DEFAULT_FLEET

# -- Stub registry -----------------------------------------------------------


@dataclass
class _StubKeyRecord:
    """Minimal duck-typed registry row.

    Mirrors :class:`pinky_identity.registry.IdentityKeyRecord` enough for
    the signer's :class:`RegistryLike` protocol — kid, fingerprint,
    status, public_key. The real registry has many more fields; the
    signer doesn't read them.
    """

    kid: str
    fingerprint: str
    status: str
    public_key: bytes


class _StubRegistry:
    """In-memory registry stub for DaemonSigner tests.

    Maps kid → _StubKeyRecord. Supports get_key and get_active_key.
    """

    def __init__(self) -> None:
        self._rows: dict[str, _StubKeyRecord] = {}
        # (fleet, agent, purpose) → kid for the active key.
        self._active: dict[tuple[str, str, str], str] = {}

    def add(
        self,
        keypair,
        *,
        fleet: str = DEFAULT_FLEET,
        agent_name: str,
        purpose: str = PURPOSE_SERVICE_AUTH,
        status: str = KEY_ACTIVE,
    ) -> str:
        kid = kid_from_public_key(keypair.public)
        row = _StubKeyRecord(
            kid=kid,
            fingerprint=fingerprint(keypair.public),
            status=status,
            public_key=keypair.public.raw,
        )
        self._rows[kid] = row
        if status == KEY_ACTIVE:
            self._active[(fleet, agent_name, purpose)] = kid
        return kid

    def set_status(self, kid: str, status: str) -> None:
        self._rows[kid].status = status
        # If we're moving off active, drop from the active index.
        if status != KEY_ACTIVE:
            stale = [k for k, v in self._active.items() if v == kid]
            for k in stale:
                del self._active[k]

    # -- RegistryLike protocol --

    def get_key(self, kid: str):
        return self._rows.get(kid)

    def get_active_key(self, fleet: str, agent_name: str, purpose: str):
        kid = self._active.get((fleet, agent_name, purpose))
        if kid is None:
            return None
        return self._rows[kid]


# -- Fixtures ----------------------------------------------------------------


@pytest.fixture
def device_key():
    return DeviceKey.from_bytes(secrets.token_bytes(DEVICE_KEY_BYTES))


@pytest.fixture
def store(tmp_path, device_key):
    s = EncryptedSignerStore(db_path=tmp_path / "ss.db", device_key=device_key)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def registry():
    return _StubRegistry()


@pytest.fixture
def signer(registry, store):
    return DaemonSigner(registry=registry, signer_store=store)


def _prep_get(url: str = "https://api.example.com/v1/things") -> requests.PreparedRequest:
    return requests.Request("GET", url).prepare()


# -- Construction ------------------------------------------------------------


def test_construct_rejects_non_store(registry):
    with pytest.raises(TypeError):
        DaemonSigner(registry=registry, signer_store={"not": "a store"})  # type: ignore[arg-type]


def test_construct_accepts_duck_typed_registry(store):
    # The Protocol is runtime_checkable but isinstance is permissive on
    # Protocol — we don't enforce. Any object with the right methods works.
    class Minimal:
        def get_key(self, kid): return None  # noqa: E704
        def get_active_key(self, f, a, p): return None  # noqa: E704

    DaemonSigner(registry=Minimal(), signer_store=store)


def test_defaults_match_registry_constants():
    # If these drift from registry's defaults, downstream callers see
    # surprising behaviour. The constants are intentionally duplicated
    # in signer.py to avoid the runtime registry import.
    from pinky_identity.signer import DEFAULT_FLEET, KEY_ACTIVE, PURPOSE_SERVICE_AUTH

    assert KEY_ACTIVE == "active"
    assert PURPOSE_SERVICE_AUTH == "service-auth"
    assert DEFAULT_FLEET == "pinky"


# -- sign_request_by_kid: happy path ----------------------------------------


def test_sign_request_by_kid_round_trips(signer, registry, store):
    kp = generate_keypair()
    kid = registry.add(kp, agent_name="alpha")
    store.put_keypair(kp)

    req = _prep_get()
    returned_kid = signer.sign_request_by_kid(req, kid=kid)
    assert returned_kid == kid

    resolver = PinkyKeyResolver.from_signing_keypairs([kp])
    result = verify_request(req, key_resolver=resolver)
    assert result.parameters["keyid"] == kid
    assert result.parameters["tag"] == DEFAULT_TAG


def test_sign_request_for_agent_resolves_via_registry(signer, registry, store):
    kp = generate_keypair()
    registry.add(kp, agent_name="alpha")
    store.put_keypair(kp)

    req = _prep_get()
    kid = signer.sign_request_for_agent(req, agent_name="alpha")
    assert kid == kid_from_public_key(kp.public)


# -- Status gating ----------------------------------------------------------


@pytest.mark.parametrize("blocked_status", ["pending", "retired", "revoked"])
def test_sign_request_by_kid_refuses_non_active_status(
    signer, registry, store, blocked_status
):
    """Murzik's PR-4b heads-up: the signer must refuse to sign when the
    registry status is not 'active', even if the wrapped seed is still
    in the signer store. Regression test up-front for that invariant.
    """
    kp = generate_keypair()
    kid = registry.add(kp, agent_name="alpha", status=blocked_status)
    store.put_keypair(kp)

    req = _prep_get()
    with pytest.raises(KeyNotActiveError) as exc:
        signer.sign_request_by_kid(req, kid=kid)
    assert exc.value.kid == kid
    assert exc.value.status == blocked_status


def test_status_change_to_revoked_blocks_subsequent_signing(signer, registry, store):
    """Reproduce the revoke-then-still-stored scenario explicitly."""
    kp = generate_keypair()
    kid = registry.add(kp, agent_name="alpha")
    store.put_keypair(kp)

    # First sign: works.
    signer.sign_request_by_kid(_prep_get(), kid=kid)

    # Registry revokes the key. Wrapped row still in the signer store.
    registry.set_status(kid, "revoked")
    with pytest.raises(KeyNotActiveError, match="revoked"):
        signer.sign_request_by_kid(_prep_get(), kid=kid)


def test_signer_error_is_signature_error_subclass(signer, registry, store):
    kp = generate_keypair()
    kid = registry.add(kp, agent_name="alpha", status="revoked")
    store.put_keypair(kp)
    with pytest.raises(SignatureError):
        signer.sign_request_by_kid(_prep_get(), kid=kid)


# -- Missing rows -----------------------------------------------------------


def test_sign_request_by_kid_raises_when_registry_has_no_row(signer, store):
    kp = generate_keypair()
    store.put_keypair(kp)  # signer store has it, registry does not
    with pytest.raises(KeyNotRegisteredError):
        signer.sign_request_by_kid(_prep_get(), kid=kid_from_public_key(kp.public))


def test_sign_request_by_kid_raises_when_signer_store_has_no_row(
    signer, registry
):
    kp = generate_keypair()
    kid = registry.add(kp, agent_name="alpha")  # registry has it, store does not
    with pytest.raises(KeyNotInStoreError):
        signer.sign_request_by_kid(_prep_get(), kid=kid)


def test_resolve_active_kid_raises_when_no_active(signer, registry, store):
    with pytest.raises(KeyNotRegisteredError, match="alpha"):
        signer.resolve_active_kid(agent_name="alpha")


# -- Fingerprint mismatch ---------------------------------------------------


def test_fingerprint_mismatch_between_registry_and_store_raises(
    signer, registry, store
):
    """Defensive: if the registry and signer store ever disagree on
    fingerprint for the same kid, refuse — that's corruption."""
    kp = generate_keypair()
    other = generate_keypair()
    kid = registry.add(kp, agent_name="alpha")
    store.put_keypair(kp)

    # Tamper with the registry stub to claim a different fingerprint.
    registry._rows[kid].fingerprint = fingerprint(other.public)

    with pytest.raises(KidFingerprintMismatchError):
        signer.sign_request_by_kid(_prep_get(), kid=kid)


# -- get_public_key_resolver -------------------------------------------------


def test_get_public_key_resolver_only_includes_active(signer, registry, store):
    active = generate_keypair()
    retired = generate_keypair()
    not_in_registry = generate_keypair()

    registry.add(active, agent_name="alpha")
    registry.add(retired, agent_name="beta", status="retired")
    store.put_keypair(active)
    store.put_keypair(retired)
    store.put_keypair(not_in_registry)

    resolver = signer.get_public_key_resolver()
    active_kid = kid_from_public_key(active.public)
    retired_kid = kid_from_public_key(retired.public)
    orphan_kid = kid_from_public_key(not_in_registry.public)

    assert active_kid in resolver.public_keys
    assert retired_kid not in resolver.public_keys
    assert orphan_kid not in resolver.public_keys


# -- fleet / purpose overrides ----------------------------------------------


def test_sign_request_for_agent_respects_custom_fleet_and_purpose(
    registry, store
):
    """Provisioning a key on a non-default fleet+purpose tuple should
    resolve correctly when the signer is configured to that scope."""
    signer_custom = DaemonSigner(
        registry=registry,
        signer_store=store,
        fleet="other-fleet",
        purpose="other-purpose",
    )
    kp = generate_keypair()
    registry.add(kp, fleet="other-fleet", agent_name="zeta", purpose="other-purpose")
    store.put_keypair(kp)

    req = _prep_get()
    kid = signer_custom.sign_request_for_agent(req, agent_name="zeta")
    assert kid == kid_from_public_key(kp.public)


def test_sign_request_for_agent_per_call_overrides(signer, registry, store):
    kp = generate_keypair()
    registry.add(kp, fleet="x", agent_name="zeta", purpose="y")
    store.put_keypair(kp)

    req = _prep_get()
    kid = signer.sign_request_for_agent(
        req, agent_name="zeta", fleet="x", purpose="y"
    )
    assert kid == kid_from_public_key(kp.public)


# -- Signing options propagate ----------------------------------------------


def test_sign_request_by_kid_honors_explicit_tag(signer, registry, store):
    kp = generate_keypair()
    kid = registry.add(kp, agent_name="alpha")
    store.put_keypair(kp)

    req = _prep_get()
    signer.sign_request_by_kid(req, kid=kid, tag="custom-tag-v9")
    assert 'tag="custom-tag-v9"' in req.headers["Signature-Input"]


def test_sign_request_by_kid_honors_explicit_covered_components(
    signer, registry, store
):
    kp = generate_keypair()
    kid = registry.add(kp, agent_name="alpha")
    store.put_keypair(kp)

    req = _prep_get()
    signer.sign_request_by_kid(
        req, kid=kid, covered_components=("@method", "@target-uri")
    )
    sig_input = req.headers["Signature-Input"]
    assert '"@method"' in sig_input
    assert '"@target-uri"' in sig_input
    # @authority should NOT be there since we omitted it.
    assert '"@authority"' not in sig_input


# -- Error class structure --------------------------------------------------


def test_all_signer_errors_inherit_from_signer_error():
    for cls in [
        KeyNotActiveError,
        KeyNotInStoreError,
        KeyNotRegisteredError,
        KidFingerprintMismatchError,
    ]:
        assert issubclass(cls, SignerError)
