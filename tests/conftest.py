import flask
import pytest
from octoprint.events import Events

import octoprint_prusa_livez as plugin_module
from octoprint_prusa_livez import PrusaLiveZPlugin

from fakes import FakeFirmware, FakePrinter, FakeSettings, SyncThread


@pytest.fixture
def firmware():
    return FakeFirmware()


@pytest.fixture
def plugin(firmware, mocker, tmp_path, monkeypatch):
    """A plugin wired to a fake printer, with threads run in place."""
    monkeypatch.setattr(plugin_module.threading, "Thread", SyncThread)
    p = PrusaLiveZPlugin()
    printer = FakePrinter(firmware)
    printer.plugin = p
    p._printer = printer
    p._settings = FakeSettings(p.get_settings_defaults())
    p._plugin_manager = mocker.Mock()
    p._file_manager = mocker.Mock()
    p._file_manager.path_on_disk.side_effect = lambda origin, path: path
    p._logger = mocker.Mock()
    p._identifier = "prusa_livez"
    p._plugin_version = "1.0.0"
    p._data_folder = str(tmp_path)
    p.get_plugin_data_folder = lambda: str(tmp_path)
    return p


@pytest.fixture
def connected(plugin):
    """The plugin after the printer reported Prusa firmware 3.14.1 and the sheets were read."""
    plugin.on_event(Events.FIRMWARE_DATA, {"name": "Prusa-Firmware 3.14.1+8237_74a577bc0", "data": {}})
    return plugin


@pytest.fixture
def permissions(mocker):
    """Grant (or, by changing the return values, refuse) OctoPrint's permissions."""
    perms = mocker.patch.object(plugin_module, "Permissions")
    perms.STATUS.can.return_value = True
    perms.PLUGIN_PRUSA_LIVEZ_EDIT.can.return_value = True
    return perms


@pytest.fixture
def api(connected, permissions):
    """Call the plugin's SimpleApi like the browser does. Returns (status, json)."""
    app = flask.Flask(__name__)

    def call(command, **data):
        with app.test_request_context():
            response = connected.on_api_command(command, data)
            return response.status_code, response.get_json()

    call.plugin = connected
    call.app = app
    return call
