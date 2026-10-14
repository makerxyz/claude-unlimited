from datetime import datetime, timezone

import claude_unlimited.gateway as gateway
import claude_unlimited.profiles as profiles
from claude_unlimited.config import Pool, Profile, save_pool
from claude_unlimited.oauth_credential import StoredOAuthCredential, encode, decode
from claude_unlimited.oauth_login import LoginTokens, OAuthLoginError


class Store:
    def __init__(self):
        self.tokens = {'a': encode(StoredOAuthCredential('old-access', 'old-refresh', 1))}
    def get_token(self, key):
        return self.tokens[key]
    def set_token(self, key, value):
        self.tokens[key] = value
    def delete_token(self, key):
        self.tokens.pop(key, None)


def setup(monkeypatch):
    store = Store()
    monkeypatch.setattr(gateway, 'secret_store', store)
    monkeypatch.setattr(profiles, 'secret_store', store)
    save_pool(Pool(profiles=[Profile(id='a', name='A', automatic=True)]))
    return store


def test_second_gateway_adopts_rotated_token_instead_of_replaying_stale_refresh(monkeypatch):
    store = setup(monkeypatch)
    exchanges = []
    def exchange(token):
        exchanges.append(token)
        return LoginTokens('new-access', 'new-refresh', 9999999999999)
    monkeypatch.setattr(gateway.oauth_login, 'refresh_access_token', exchange)
    one, two = gateway.Gateway(), gateway.Gateway()
    assert one._try_refresh('a', 'old-refresh').access_token == 'new-access'
    assert two._try_refresh('a', 'old-refresh').access_token == 'new-access'
    assert exchanges == ['old-refresh']
    assert decode(store.tokens['a']).refresh_token == 'new-refresh'


def test_restart_recovers_rotated_credential_when_primary_save_failed(monkeypatch):
    store = setup(monkeypatch)
    monkeypatch.setattr(gateway.oauth_login, 'refresh_access_token', lambda t: LoginTokens('new-access', 'new-refresh', 9999999999999))
    original = profiles.update_credential
    monkeypatch.setattr(profiles, 'update_credential', lambda *a, **kw: (_ for _ in ()).throw(OSError('fixture save failure')))
    try:
        gateway.Gateway()._try_refresh('a', 'old-refresh')
    except OAuthLoginError:
        pass
    assert 'pending.a' in store.tokens
    monkeypatch.setattr(profiles, 'update_credential', original)
    monkeypatch.setattr(gateway.oauth_login, 'refresh_access_token', lambda t: (_ for _ in ()).throw(AssertionError('spent token exchanged again')))
    assert gateway.Gateway()._try_refresh('a', 'old-refresh').access_token == 'new-access'
    assert decode(store.tokens['a']).refresh_token == 'new-refresh'
    assert 'pending.a' not in store.tokens


def test_invalid_grant_is_latched_until_credential_generation_changes(monkeypatch):
    setup(monkeypatch)
    exchanges = []
    def reject(t):
        exchanges.append(t)
        raise OAuthLoginError('invalid_grant', 400, 'invalid_grant')
    monkeypatch.setattr(gateway.oauth_login, 'refresh_access_token', reject)
    gw = gateway.Gateway()
    gw.runtime_snapshot()
    gw._refresh_check_not_before.clear()
    assert gw._try_refresh('a', 'old-refresh') is None
    assert len(exchanges) == 1


def test_usage_freshness_survives_restart(monkeypatch):
    setup(monkeypatch)
    gw = gateway.Gateway()
    gw.runtime_snapshot()
    with gw._lock:
        gw._observe('a', gateway.UsageSnapshot(percent=12, resets_at=None, confidence='authoritative'), datetime.now(timezone.utc))
    gw._persist()
    gateway.runtime_state.flush()
    restarted = gateway.Gateway()
    restarted.runtime_snapshot()
    assert restarted.usage_observed_at()['a'] == gw.usage_observed_at()['a']


def test_human_login_supersedes_failed_refresh_journal(monkeypatch):
    store = setup(monkeypatch)
    gw = gateway.Gateway()
    gw._rotated_pending['a'] = ('old-refresh', LoginTokens('rotated-access', 'rotated-refresh', 9999999999999))
    store.tokens['a'] = encode(StoredOAuthCredential('human-access', 'human-refresh', 9999999999999))
    monkeypatch.setattr(gateway.oauth_login, 'refresh_access_token', lambda t: LoginTokens('renewed-human', 'new-human-refresh', 9999999999999))
    assert gw._try_refresh('a', 'human-refresh').access_token == 'renewed-human'
    assert decode(store.tokens['a']).refresh_token == 'new-human-refresh'


def test_transient_access_failure_does_not_require_oauth_when_refresh_can_recover(monkeypatch):
    setup(monkeypatch)
    gw = gateway.Gateway()
    monkeypatch.setattr(gateway.oauth_login, 'refresh_access_token', lambda t: (_ for _ in ()).throw(OAuthLoginError('temporarily unavailable', 503)))
    gw.runtime_snapshot()
    with gw._lock:
        gw._observe('a', gateway.AuthInvalid(), datetime.now(timezone.utc))
    assert gw.auth_health('a') == 'refresh_pending'


def test_plain_token_human_login_wins_over_stale_refresh_snapshot(monkeypatch):
    store = setup(monkeypatch)
    profile = profiles.list_profiles()[0]
    old = store.tokens['a']
    store.tokens['a'] = 'human-token-with-no-refresh'
    monkeypatch.setattr(gateway.oauth_login, 'refresh_access_token', lambda t: (_ for _ in ()).throw(AssertionError('old grant spent after replacement')))
    assert gateway.Gateway()._maybe_refresh_credential(profile, old) == store.tokens['a']


def test_transient_second_primary_read_failure_keeps_request_on_first_access_token(monkeypatch):
    store = setup(monkeypatch)
    profile = profiles.list_profiles()[0]
    old = store.tokens['a']
    store.get_token = lambda key: (_ for _ in ()).throw(OSError('fixture locked keychain'))
    monkeypatch.setattr(gateway.oauth_login, 'refresh_access_token', lambda t: (_ for _ in ()).throw(AssertionError('read failure must not exchange')))
    assert gateway.Gateway()._maybe_refresh_credential(profile, old) == 'old-access'


def test_unreadable_pending_journal_never_replays_source_refresh(monkeypatch):
    store = setup(monkeypatch)
    read = store.get_token
    store.get_token = lambda key: (_ for _ in ()).throw(OSError('fixture locked journal')) if key.startswith('pending.') else read(key)
    monkeypatch.setattr(gateway.oauth_login, 'refresh_access_token', lambda t: (_ for _ in ()).throw(AssertionError('ambiguous journal must not exchange')))
    try:
        gateway.Gateway()._try_refresh('a', 'old-refresh')
    except OAuthLoginError as exc:
        assert 'recovery record is unavailable' in str(exc)
    else:
        raise AssertionError('expected deferred refresh')


def test_malformed_pending_journal_cannot_be_mistaken_for_absence(monkeypatch):
    import pytest
    store = setup(monkeypatch)
    monkeypatch.setattr(gateway.oauth_login, 'refresh_access_token', lambda t: (_ for _ in ()).throw(AssertionError('corrupt journal replayed spent refresh')))
    for journal in ['{}', '[]', 'null', '{"source_refresh_token":123}', '{"access_token":"a","refresh_token":"r","source_refresh_token":"s","expires_at":false}', '{"access_token":"a","refresh_token":"r","source_refresh_token":"s","expires_at":NaN}']:
        store.tokens['pending.a'] = journal
        with pytest.raises(OAuthLoginError, match='recovery record is invalid'):
            gateway.Gateway()._try_refresh('a', 'old-refresh')
