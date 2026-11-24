from vinylpilot.mqtt import MqttPublisher, MqttSettings, Status, _slug


def test_slug():
    assert _slug("VinylPilot-Zero") == "vinylpilot_zero"


def test_discovery_entities_are_unique_and_complete():
    pub = MqttPublisher(MqttSettings(host="x"), Status(), name="VinylPilot",
                        on_threshold=lambda v: None, on_hold=lambda v: None)
    entities = pub._entities()
    keys = [key for _, key, _ in entities]
    assert len(keys) == len(set(keys))
    for required in ("turntable_connected", "record_playing", "streaming", "cpu",
                     "temperature", "wifi_rssi", "level_left", "level_right", "version"):
        assert required in keys


def test_cover_never_empty():
    from vinylpilot.mqtt import COVER_PLACEHOLDER
    pub = MqttPublisher(MqttSettings(host="x"), Status(), name="VinylPilot",
                        on_threshold=lambda v: None, on_hold=lambda v: None)
    cover = next(cfg for comp, key, cfg in pub._entities() if key == "cover")
    assert COVER_PLACEHOLDER in cover["url_template"]
