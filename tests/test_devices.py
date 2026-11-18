from vinylpilot.devices import list_usb_capture_devices

CARDS = """ 0 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0
                      vc4-hdmi-0
 2 [PSHX500        ]: USB-Audio - PS-HX500
                      Sony Corporation PS-HX500 at usb-xhci-hcd.1-1, high speed
"""


def test_finds_ps_hx500(tmp_path):
    (tmp_path / "cards").write_text(CARDS)
    (tmp_path / "card2").mkdir()
    (tmp_path / "card2" / "stream0").write_text("Sony PS-HX500\n\nCapture:\n  Status: Stop\n")
    devices = list_usb_capture_devices(tmp_path)
    assert len(devices) == 1
    assert devices[0].alsa_name == "hw:CARD=PSHX500,DEV=0"
    assert devices[0].name == "PS-HX500"


def test_ignores_playback_only(tmp_path):
    (tmp_path / "cards").write_text(CARDS)
    (tmp_path / "card2").mkdir()
    (tmp_path / "card2" / "stream0").write_text("Playback:\n")
    assert list_usb_capture_devices(tmp_path) == []
