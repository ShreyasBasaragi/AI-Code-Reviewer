"""GitHub/webhook credential storage using macOS Keychain or explicit memory mode."""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
import threading
from typing import Optional


class CredentialStoreError(RuntimeError):
    pass


class MacKeychain:
    SERVICE = "com.ai-code-reviewer.credentials"

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise CredentialStoreError("secure_credential_store_unavailable")
        security_path = ctypes.util.find_library("Security")
        corefoundation_path = ctypes.util.find_library("CoreFoundation")
        if not security_path or not corefoundation_path:
            raise CredentialStoreError("secure_credential_store_unavailable")
        self._security = ctypes.CDLL(security_path)
        self._corefoundation = ctypes.CDLL(corefoundation_path)
        self._configure()

    def _configure(self) -> None:
        u32, cstr, cvoid, ppvoid = ctypes.c_uint32, ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        self._security.SecKeychainFindGenericPassword.argtypes = [cvoid, u32, cstr, u32, cstr,
                                                                  ctypes.POINTER(u32), ppvoid, ppvoid]
        self._security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainAddGenericPassword.argtypes = [cvoid, u32, cstr, u32, cstr,
                                                                 u32, cvoid, ppvoid]
        self._security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainItemModifyAttributesAndData.argtypes = [cvoid, cvoid, u32, cvoid]
        self._security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self._security.SecKeychainItemDelete.argtypes = [cvoid]
        self._security.SecKeychainItemDelete.restype = ctypes.c_int32
        self._security.SecKeychainItemFreeContent.argtypes = [cvoid, cvoid]
        self._security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self._corefoundation.CFRelease.argtypes = [cvoid]
        self._corefoundation.CFRelease.restype = None

    def _find(self, name: str):
        service, account = self.SERVICE.encode(), name.encode()
        length, data, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            None, len(service), service, len(account), account,
            ctypes.byref(length), ctypes.byref(data), ctypes.byref(item))
        if status == -25300:  # errSecItemNotFound
            return None
        if status != 0:
            raise CredentialStoreError("secure_credential_store_read_failed")
        try:
            return item, ctypes.string_at(data, length.value).decode("utf-8")
        finally:
            self._security.SecKeychainItemFreeContent(None, data)

    def get(self, name: str) -> Optional[str]:
        result = self._find(name)
        if result is None:
            return None
        item, value = result
        self._corefoundation.CFRelease(item)
        return value

    def set(self, name: str, value: str) -> None:
        encoded_service, account = self.SERVICE.encode(), name.encode()
        password = value.encode("utf-8")
        found = self._find(name)
        if found is not None:
            item, _old_value = found
            try:
                buffer = ctypes.create_string_buffer(password)
                status = self._security.SecKeychainItemModifyAttributesAndData(item, None, len(password), buffer)
            finally:
                self._corefoundation.CFRelease(item)
            if status != 0:
                raise CredentialStoreError("secure_credential_store_write_failed")
            return
        item = ctypes.c_void_p()
        buffer = ctypes.create_string_buffer(password)
        status = self._security.SecKeychainAddGenericPassword(
            None, len(encoded_service), encoded_service, len(account), account,
            len(password), buffer, ctypes.byref(item))
        if status != 0:
            raise CredentialStoreError("secure_credential_store_write_failed")
        if item:
            self._corefoundation.CFRelease(item)

    def delete(self, name: str) -> None:
        found = self._find(name)
        if found is None:
            return
        item, _value = found
        try:
            status = self._security.SecKeychainItemDelete(item)
        finally:
            self._corefoundation.CFRelease(item)
        if status not in {0, -25300}:
            raise CredentialStoreError("secure_credential_store_delete_failed")


class CredentialStore:
    ENVIRONMENT = {
        "github_token": "GITHUB_TOKEN",
        "github_actions_token": "GITHUB_ACTIONS_TOKEN",
        "webhook_secret": "GITHUB_WEBHOOK_SECRET",
    }

    def __init__(self, keychain=None) -> None:
        self._lock = threading.RLock()
        self._memory: dict[str, Optional[str]] = {}
        self._keychain = keychain
        self._keychain_checked = keychain is not None

    def _persistent_store(self):
        if not self._keychain_checked:
            try:
                self._keychain = MacKeychain()
            except CredentialStoreError:
                self._keychain = None
            self._keychain_checked = True
        return self._keychain

    @property
    def persistence_available(self) -> bool:
        return self._persistent_store() is not None

    def get(self, name: str) -> Optional[str]:
        with self._lock:
            if name in self._memory:
                return self._memory[name]
            store = self._persistent_store()
            if store is not None:
                value = store.get(name)
                if value:
                    return value
            env_name = self.ENVIRONMENT.get(name)
            return os.getenv(env_name) if env_name else None

    def set(self, name: str, value: str, *, persistent: bool) -> str:
        if name not in self.ENVIRONMENT or not isinstance(value, str) or not value or "\x00" in value or len(value) > 4096:
            raise CredentialStoreError("credential_value_invalid")
        with self._lock:
            if persistent:
                store = self._persistent_store()
                if store is None:
                    raise CredentialStoreError("secure_credential_store_unavailable_choose_session_only")
                store.set(name, value)
                self._memory.pop(name, None)
                return "macos_keychain"
            self._memory[name] = value
            return "session_only_process_memory"

    def remove(self, name: str) -> str:
        if name not in self.ENVIRONMENT:
            raise CredentialStoreError("credential_name_invalid")
        with self._lock:
            env_name = self.ENVIRONMENT[name]
            if os.getenv(env_name):
                raise CredentialStoreError("credential_is_environment_managed_remove_it_from_server_environment")
            store = self._persistent_store()
            if store is not None:
                store.delete(name)
            self._memory.pop(name, None)
            self._memory[name] = None
            return "removed"

    def status(self) -> dict[str, object]:
        def configured(name: str):
            try:
                return bool(self.get(name))
            except CredentialStoreError:
                return None
        try:
            persistent = self.persistence_available
        except CredentialStoreError:
            persistent = False
        return {
            "github_token_configured": configured("github_token"),
            "github_actions_token_configured": configured("github_actions_token"),
            "webhook_secret_configured": configured("webhook_secret"),
            "persistent_storage_available": persistent,
            "storage_mode": "macos_keychain_available" if persistent else "session_only_available",
        }


CREDENTIALS = CredentialStore()
