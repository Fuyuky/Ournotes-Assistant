"""macOS login Keychain via Security.framework; no secret in a command line."""
from __future__ import annotations

import ctypes as C

NOT_FOUND = -25300


class Keychain:
    def __init__(self, service):
        self.service = service.encode("utf-8")
        self.security = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.core = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.core.CFRelease.argtypes = [C.c_void_p]
        self.core.CFRelease.restype = None
        signatures = {
            "SecKeychainFindGenericPassword": [C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32,
                                               C.c_void_p, C.POINTER(C.c_uint32),
                                               C.POINTER(C.c_void_p), C.POINTER(C.c_void_p)],
            "SecKeychainAddGenericPassword": [C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32,
                                              C.c_void_p, C.c_uint32, C.c_void_p,
                                              C.POINTER(C.c_void_p)],
            "SecKeychainItemModifyAttributesAndData": [C.c_void_p, C.c_void_p, C.c_uint32, C.c_void_p],
            "SecKeychainItemDelete": [C.c_void_p],
            "SecKeychainItemFreeContent": [C.c_void_p, C.c_void_p],
        }
        for name, arguments in signatures.items():
            method = getattr(self.security, name)
            method.argtypes, method.restype = arguments, C.c_int32

    @staticmethod
    def check(status):
        if status:
            raise OSError(f"macOS Keychain 操作失败（{status}）；请检查当前用户钥匙串权限。")

    def find(self, name):
        account = name.encode("utf-8")
        length, password, item = C.c_uint32(), C.c_void_p(), C.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(
            None, len(self.service), self.service, len(account), account,
            C.byref(length), C.byref(password), C.byref(item))
        if status == NOT_FOUND:
            return None, None
        self.check(status)
        try:
            raw = C.string_at(password, length.value)
        finally:
            self.security.SecKeychainItemFreeContent(None, password)
        return raw, item

    def load(self, name):
        raw, item = self.find(name)
        if item:
            self.core.CFRelease(item)
        return raw

    def save(self, name, raw):
        _, item = self.find(name)
        account = name.encode("utf-8")
        try:
            if item:
                status = self.security.SecKeychainItemModifyAttributesAndData(item, None, len(raw), raw)
            else:
                status = self.security.SecKeychainAddGenericPassword(
                    None, len(self.service), self.service, len(account), account, len(raw), raw, None)
            self.check(status)
        finally:
            if item:
                self.core.CFRelease(item)

    def delete(self, name):
        _, item = self.find(name)
        if item:
            try:
                self.check(self.security.SecKeychainItemDelete(item))
            finally:
                self.core.CFRelease(item)
