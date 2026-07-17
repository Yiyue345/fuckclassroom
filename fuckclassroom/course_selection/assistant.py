from __future__ import annotations

import base64
import json
import re
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, ProxyHandler, Request, build_opener

from ..auth.cas import LoginVerificationBroker
from ..auth.credentials import SelectionCredentialStore
from ..core.config import AppConfig
from .api import SelectionApiMixin
from .browser import SelectionBrowserMixin
from .proxy import Hy2ProxyManager
from .session import SelectionSessionMixin

class CourseSelectionAssistant(
    SelectionSessionMixin,
    SelectionApiMixin,
    SelectionBrowserMixin,
):
    def __init__(
        self,
        config: AppConfig | None = None,
        hy2_proxy: Hy2ProxyManager | None = None,
        credential_store: SelectionCredentialStore | None = None,
        verification_broker: LoginVerificationBroker | None = None,
        login_lock: threading.Lock | None = None,
    ) -> None:
        self.config = config or AppConfig()
        self.hy2_proxy = hy2_proxy or Hy2ProxyManager(self.config)
        self.session_dir = self.config.data_dir / "webvpn"
        self.credential_store = credential_store or SelectionCredentialStore(
            self.session_dir / "credentials.json"
        )
        self.storage_state_path = self.session_dir / "storage_state.json"
        self.session_meta_path = self.session_dir / "session_meta.json"
        self.verification_broker = verification_broker or LoginVerificationBroker()
        self.capture_dir = self.session_dir / "captures"
        self.selection_info_path = self.session_dir / "selection_info.json"
        self.selection_action_path = self.session_dir / "selection_action.json"
        self.access_mode_path = self.session_dir / "access_mode.json"
        self._access_mode = ""
        self._access_mode_checked_at = 0.0
        self._access_mode_lock = threading.Lock()
        self.session_check_path = self.session_dir / "session_check.json"
        self._selection_token = ""
        self._selection_token_expires_at = 0.0
        self._selection_token_mode = ""
        self._selection_token_lock = threading.Lock()
        self._login_lock = login_lock or threading.Lock()
        self._selection_action_lock = threading.Lock()
