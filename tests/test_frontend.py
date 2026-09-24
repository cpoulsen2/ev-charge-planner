"""Tests for opstarts-sikker indlæsning af tidsvælger-kortet."""

from __future__ import annotations

import asyncio
import os

from custom_components.ev_charge_planner import (
    _LOCAL_URL,
    _async_register_lovelace_resource,
    _copy_card_to_www,
)


class FakeResources:
    """Efterligner lovelace ResourceStorageCollection."""

    def __init__(self, items=None):
        self.items = list(items or [])
        self.loaded = False

    async def async_get_info(self):
        self.loaded = True
        return {"resources": len(self.items)}

    def async_items(self):
        assert self.loaded, "samlingen skal indlæses før async_items"
        return self.items

    async def async_create_item(self, data):
        item = {"id": f"id{len(self.items)}", "type": data["res_type"], "url": data["url"]}
        self.items.append(item)
        return item

    async def async_update_item(self, item_id, updates):
        for item in self.items:
            if item["id"] == item_id:
                item.update(updates)
                return item
        raise KeyError(item_id)


class FakeLovelaceData:
    def __init__(self, resources):
        self.resources = resources


class FakeHass:
    def __init__(self, lovelace):
        self.data = {"lovelace": lovelace}


def _run(hass, url):
    asyncio.run(_async_register_lovelace_resource(hass, url))


def test_creates_resource_when_missing():
    res = FakeResources([{"id": "a", "type": "module", "url": "/hacsfiles/x.js"}])
    _run(FakeHass(FakeLovelaceData(res)), f"{_LOCAL_URL}?v=1.0.0")
    assert res.items[-1] == {"id": "id1", "type": "module", "url": f"{_LOCAL_URL}?v=1.0.0"}
    assert len(res.items) == 2


def test_updates_version_query_without_duplicating():
    res = FakeResources([{"id": "a", "type": "module", "url": f"{_LOCAL_URL}?v=0.9.4"}])
    _run(FakeHass(FakeLovelaceData(res)), f"{_LOCAL_URL}?v=0.10.1")
    assert res.items == [{"id": "a", "type": "module", "url": f"{_LOCAL_URL}?v=0.10.1"}]


def test_same_url_is_noop():
    url = f"{_LOCAL_URL}?v=0.10.1"
    res = FakeResources([{"id": "a", "type": "module", "url": url}])
    _run(FakeHass(FakeLovelaceData(res)), url)
    assert res.items == [{"id": "a", "type": "module", "url": url}]


def test_yaml_mode_and_missing_lovelace_are_ignored():
    class YamlResources:
        async def async_get_info(self):
            raise AssertionError("må ikke kaldes i YAML-mode")

    _run(FakeHass(FakeLovelaceData(YamlResources())), _LOCAL_URL)
    _run(FakeHass(None), _LOCAL_URL)


def test_legacy_dict_layout():
    res = FakeResources()
    _run(FakeHass({"resources": res}), _LOCAL_URL)
    assert res.items[0]["url"] == _LOCAL_URL


def test_copy_card_to_www(tmp_path):
    src = tmp_path / "evcp-time-picker.js"
    src.write_bytes(b"v1")
    www = tmp_path / "www"  # findes ikke endnu → oprettes
    _copy_card_to_www(str(src), str(www))
    dest = www / "ev_charge_planner" / "evcp-time-picker.js"
    assert dest.read_bytes() == b"v1"

    mtime = os.stat(dest).st_mtime_ns
    _copy_card_to_www(str(src), str(www))  # uændret → skrives ikke igen
    assert os.stat(dest).st_mtime_ns == mtime

    src.write_bytes(b"v2")
    _copy_card_to_www(str(src), str(www))
    assert dest.read_bytes() == b"v2"
