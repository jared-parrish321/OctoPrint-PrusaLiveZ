$(function () {
    var PLUGIN = "prusa_livez";

    function PrusaLiveZViewModel(parameters) {
        var self = this;
        self.loginState = parameters[0];
        self.access = parameters[1];
        self.printerState = parameters[2];

        self.state = ko.observable(null);
        self.lastError = ko.observable(null);
        self.busy = ko.observable(false);
        self.selectedIndex = ko.observable(null);
        self.editZ = ko.observable("");
        self.editName = ko.observable("");
        self.showHistory = ko.observable(false);
        self.ladderEnabled = ko.observable(false);
        self.ladderRows = ko.observableArray([]);
        self.pickedBand = ko.observable(null);
        self._ladderSync = false;

        // -- derived state ------------------------------------------------

        function st(key, fallback) {
            var s = self.state();
            return s && s[key] !== undefined && s[key] !== null ? s[key] : fallback;
        }

        self.sheets = ko.pureComputed(function () {
            return st("sheets", []).filter(function (s) { return s; });
        });
        self.supported = ko.pureComputed(function () {
            return st("firmware", {}).supported === true;
        });
        self.refreshing = ko.pureComputed(function () { return st("refreshing", false); });
        self.history = ko.pureComputed(function () { return st("history", []); });
        self.shiftActive = ko.pureComputed(function () { return st("shift_active", false); });
        self.nudge = ko.pureComputed(function () { return st("nudge", 0); });
        self.pending = ko.pureComputed(function () { return st("pending", null); });
        self.jobIsLadder = ko.pureComputed(function () {
            var job = st("job", null);
            return !!(job && job.ladder);
        });
        self.activeSheet = ko.pureComputed(function () {
            return self.sheets().filter(function (s) { return s.active; })[0] || null;
        });
        self.selected = ko.pureComputed(function () {
            var index = self.selectedIndex();
            return self.sheets().filter(function (s) { return s.index === index; })[0] || null;
        });
        self.canEdit = ko.pureComputed(function () {
            var perm = self.access.permissions.PLUGIN_PRUSA_LIVEZ_EDIT;
            return !!perm && self.loginState.hasPermission(perm);
        });
        self.idle = ko.pureComputed(function () {
            return self.supported() && !self.shiftActive() && !self.busy() &&
                self.printerState.isOperational() && !self.printerState.isPrinting() &&
                !self.printerState.isPaused() && !self.printerState.isPausing() &&
                !self.printerState.isCancelling();
        });
        self.firmwareNote = ko.pureComputed(function () {
            var fw = st("firmware", {});
            if (!self.printerState.isOperational()) return gettext("Connect the printer to read Live Z.");
            if (fw.supported === false) {
                return _.sprintf(gettext("Live Z needs Prusa firmware 3.13.0 or later. This printer reports: %(name)s"),
                    { name: fw.name || gettext("unknown firmware") });
            }
            if (fw.supported !== true) return gettext("Waiting for the printer's firmware report...");
            return null;
        });

        // -- formatting -------------------------------------------------------

        self.fmt = function (z) {
            if (z === null || z === undefined || isNaN(z)) return "-";
            return Number(z).toFixed(3);
        };
        self.fmtSigned = function (z) {
            var text = Number(z || 0).toFixed(3);
            return z > 0 ? "+" + text : text;
        };
        self.fmtTime = function (t) {
            return t ? new Date(t * 1000).toLocaleString() : "";
        };
        self.steps = function () { return st("steps_per_mm", 400); };
        self.toRaw = function (mm) { return Math.round(mm * self.steps()); };

        self.describe = function (h) {
            var who = h.user ? " (" + h.user + ")" : "";
            var note = h.note ? " [" + h.note + "]" : "";
            if (h.field === "z") {
                return _.sprintf(gettext("Sheet %(s)s Live Z %(a)s → %(b)s"), {
                    s: h.sheet + 1, a: self.fmt(h.old / self.steps()), b: self.fmt(h.new / self.steps())
                }) + note + who;
            }
            if (h.field === "name") {
                return _.sprintf(gettext("Sheet %(s)s renamed %(a)s → %(b)s"), {
                    s: h.sheet + 1, a: h.old || "-", b: h.new
                }) + note + who;
            }
            return _.sprintf(gettext("Active sheet %(a)s → %(b)s"), {
                a: h.old === null ? "-" : h.old + 1, b: h.new + 1
            }) + note + who;
        };

        // -- editor -------------------------------------------------------------

        self.select = function (index) {
            self.selectedIndex(index);
            var s = self.selected();
            self.editZ(s && s.initialized ? self.fmt4(s.z) : "");
            self.editName(s ? s.name : "");
        };
        self.fmt4 = function (z) { return Number(z).toFixed(4).replace(/0$/, ""); };

        self.parsedZ = ko.pureComputed(function () {
            var v = parseFloat(String(self.editZ()).replace(",", "."));
            return isNaN(v) ? null : v;
        });
        self.zChanged = ko.pureComputed(function () {
            var s = self.selected(), v = self.parsedZ();
            return !!s && v !== null && self.toRaw(v) !== s.raw;
        });
        self.nameChanged = ko.pureComputed(function () {
            var s = self.selected();
            return !!s && $.trim(self.editName()) !== "" && $.trim(self.editName()) !== s.name;
        });
        self.editHint = ko.pureComputed(function () {
            var s = self.selected(), v = self.parsedZ();
            if (!s || v === null) return "";
            var raw = self.toRaw(v);
            if (raw === -1) return gettext("-0.0025 can't be stored; it will be saved as the next step past it.");
            if (raw > 0 || raw < -3999) return gettext("Allowed range: -9.9975 to 0.");
            return "";
        });

        self.step = function (delta) {
            var v = self.parsedZ();
            if (v === null) { var s = self.selected(); v = s ? s.z : 0; }
            var raw = self.toRaw(v + delta);
            if (raw === -1) raw = delta < 0 ? -2 : 0; // skip the "empty sheet" marker
            raw = Math.max(-3999, Math.min(0, raw));
            self.editZ(self.fmt4(raw / self.steps()));
        };

        self.saveZ = function () {
            var s = self.selected(), v = self.parsedZ();
            if (!s || v === null) return;
            self.command("set_z", { sheet: s.index, z: v });
        };
        self.saveName = function () {
            var s = self.selected();
            if (!s) return;
            self.command("rename", { sheet: s.index, name: $.trim(self.editName()) });
        };
        self.activate = function () {
            var s = self.selected();
            if (!s) return;
            self.command("activate", { sheet: s.index });
        };
        self.undo = function (h) {
            showConfirmationDialog({
                message: _.escape(_.sprintf(gettext("Undo: %(what)s?"), { what: self.describe(h) })),
                proceed: gettext("Undo"),
                onproceed: function () { self.command("undo", { id: h.id }); }
            });
        };
        self.refresh = function () { self.command("refresh", {}); };

        self.doNudge = function (delta) { self.command("nudge", { delta: delta }); };
        self.nudgeReset = function () { self.command("nudge_reset", {}); };

        // -- ladder ---------------------------------------------------------------

        self.ladderInfo = ko.pureComputed(function () {
            var l = st("ladder", null);
            return l && l.info ? l.info : null;
        });
        self.ladderDetected = ko.pureComputed(function () {
            var info = self.ladderInfo();
            return !!(info && info.detected);
        });
        self.ladderNamed = ko.pureComputed(function () {
            var info = self.ladderInfo();
            return !!(info && info.named);
        });
        self.ladderDirty = ko.pureComputed(function () {
            var l = st("ladder", null);
            if (!l) return false;
            var rows = self.ladderRows();
            if (rows.length !== (l.offsets || []).length) return true;
            for (var i = 0; i < rows.length; i++) {
                var v = parseFloat(rows[i].offset());
                if (isNaN(v) || Math.abs(v - l.offsets[i]) > 1e-9) return true;
            }
            return false;
        });
        self.ladderLiveZ = function (offset) {
            var a = self.activeSheet(), v = parseFloat(offset);
            if (!a || isNaN(v)) return "-";
            return self.fmt(a.z + v);
        };
        self._setLadderRows = function (offsets) {
            var bands = offsets.length;
            self.ladderRows(offsets.map(function (o, i) {
                return {
                    number: i + 1,
                    where: i === 0 ? gettext("front") : (i === bands - 1 ? gettext("back") : ""),
                    offset: ko.observable(String(o))
                };
            }));
        };
        self.ladderDefaults = function () {
            var info = self.ladderInfo();
            if (!info) return;
            var n = info.bands, mid = (n - 1) / 2, out = [];
            for (var i = 0; i < n; i++) out.push(Math.round((i - mid) * 0.01 * 10000) / 10000);
            self._setLadderRows(out);
        };
        self.ladderSave = function () {
            var offsets = self.ladderRows().map(function (r) { return parseFloat(r.offset()); });
            self.command("ladder_config", { offsets: offsets });
        };
        self.ladderEnabled.subscribe(function (value) {
            if (self._ladderSync) return;
            var l = st("ladder", null);
            if (l && l.enabled !== value) self.command("ladder_config", { enabled: value });
        });

        // -- result after print -----------------------------------------------------

        self.resultBands = ko.pureComputed(function () {
            var p = self.pending();
            if (!p || p.kind !== "ladder") return [];
            var sheet = st("sheets", [])[p.sheet];
            var bands = p.offsets.map(function (o, i) {
                return { band: i, offset: o, newZ: sheet ? self.fmt(sheet.z + o) : "-" };
            });
            return bands.reverse(); // back of the bed at the top, like looking down at it
        });
        self.resultTitle = ko.pureComputed(function () {
            var p = self.pending();
            if (!p) return "";
            return p.kind === "ladder" ? gettext("Pick the best band") : gettext("Keep the Z nudge?");
        });
        self.resultReady = ko.pureComputed(function () {
            var p = self.pending();
            if (!p) return false;
            return p.kind === "nudge" || self.pickedBand() !== null;
        });
        self.resultSummary = ko.pureComputed(function () {
            var p = self.pending();
            if (!p) return "";
            var sheet = st("sheets", [])[p.sheet];
            var offset = p.kind === "ladder" ? (self.pickedBand() === null ? null : p.offsets[self.pickedBand()]) : p.offset;
            if (!sheet || offset === null) return "";
            return _.sprintf(gettext("Sheet %(name)s: Live Z %(a)s → %(b)s mm"), {
                name: sheet.name || "#" + (sheet.index + 1), a: self.fmt(sheet.z), b: self.fmt(sheet.z + offset)
            });
        });
        self.applyResult = function () {
            var p = self.pending();
            if (!p) return;
            self.command("apply_result", p.kind === "ladder" ? { band: self.pickedBand() } : {}, function () {
                $("#prusa_livez_result").modal("hide");
            });
        };
        self.dismissResult = function () {
            self.command("dismiss_result", {}, function () {
                $("#prusa_livez_result").modal("hide");
            });
        };

        // -- export / import -----------------------------------------------------------

        self.exportSheets = function () {
            var data = {
                exported: new Date().toISOString(),
                steps_per_mm: self.steps(),
                sheets: self.sheets().map(function (s) {
                    return { index: s.index, name: s.name, initialized: s.initialized, raw: s.raw, z: s.z, active: s.active };
                })
            };
            var blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
            var a = document.createElement("a");
            a.href = URL.createObjectURL(blob);
            a.download = "prusa-sheets-" + new Date().toISOString().slice(0, 10) + ".json";
            document.body.appendChild(a);
            a.click();
            setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
        };
        self.pickImport = function () {
            $(".prusa-livez-import").val("").click();
        };
        self.importFile = function (data, event) {
            var file = event.target.files && event.target.files[0];
            if (!file) return;
            var reader = new FileReader();
            reader.onload = function () {
                var parsed;
                try { parsed = JSON.parse(reader.result); } catch (e) {
                    self.lastError(gettext("That file isn't valid JSON."));
                    return;
                }
                var sheets = (parsed && parsed.sheets) || [];
                showConfirmationDialog({
                    message: _.escape(_.sprintf(gettext("Write names and Live Z for %(n)s sheets from %(file)s?"), { n: sheets.length, file: file.name })),
                    proceed: gettext("Import"),
                    onproceed: function () { self.command("import", { sheets: sheets, confirm: true }); }
                });
            };
            reader.readAsText(file);
        };

        // -- server --------------------------------------------------------------------

        self.command = function (command, data, done, confirmed) {
            var payload = $.extend({}, data, confirmed ? { confirm: true } : {});
            self.busy(true);
            self.lastError(null);
            OctoPrint.simpleApiCommand(PLUGIN, command, payload)
                .always(function () { self.busy(false); })
                .done(function (response) {
                    if (response && response.state) self.applyState(response.state);
                    if (response && response.message) new PNotify({ title: "Live Z", text: response.message, type: "info" });
                    if (done) done(response);
                })
                .fail(function (xhr) {
                    var body = xhr.responseJSON || {};
                    if (xhr.status === 409 && body.code === "confirm") {
                        showConfirmationDialog({
                            message: _.escape(body.error),
                            proceed: gettext("Continue"),
                            onproceed: function () { self.command(command, data, done, true); }
                        });
                        return;
                    }
                    self.lastError(body.error || (xhr.status === 403 ? gettext("You don't have permission for that.") : gettext("Request failed.")));
                });
        };

        self.applyState = function (state) {
            var before = self.pending();
            self.state(state);

            if (self.selectedIndex() === null && self.activeSheet()) {
                self.select(self.activeSheet().index);
            } else if (self.selected() && !self.zChanged() && !self.nameChanged()) {
                // keep the editor in step with the printer when nothing is being typed
                self.select(self.selectedIndex());
            }

            var l = state.ladder || {};
            self._ladderSync = true;
            self.ladderEnabled(!!l.enabled);
            self._ladderSync = false;
            if (l.info && l.info.detected && (!self.ladderRows().length || !self.ladderDirty())) {
                self._setLadderRows(l.offsets || []);
            }
            if (!l.info) self.ladderRows([]);

            var p = state.pending;
            if (p && (!before || before.kind !== p.kind || before.sheet !== p.sheet)) {
                self.pickedBand(null);
                $("#prusa_livez_result").modal("show");
            }
            if (!p) $("#prusa_livez_result").modal("hide");
        };

        self.fetch = function () {
            if (!self.loginState.isUser()) return;
            OctoPrint.simpleApiGet(PLUGIN).done(self.applyState);
        };

        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== PLUGIN || !data) return;
            if (data.type === "state" && data.state) self.applyState(data.state);
            if (data.notice === "start" && data.state) {
                var a = self.activeSheet(), job = data.state.job || {};
                if (a) {
                    new PNotify({
                        title: gettext("Printing on sheet ") + (a.name || "#" + (a.index + 1)),
                        text: _.sprintf(gettext("Live Z %(z)s mm%(ladder)s"), {
                            z: self.fmt(a.z), ladder: job.ladder ? gettext(", calibration ladder on") : ""
                        }),
                        type: "info",
                        hide: true
                    });
                }
            }
            if (data.error) self.lastError(data.error);
        };

        self.onAllBound = function () {
            // Moved only after binding, so the control tab's own view model never binds it.
            $("#prusa_livez_control").prependTo("#control-jog-general");
            self.fetch();
        };
        self.onUserLoggedIn = self.fetch;
        self.onUserLoggedOut = function () { self.state(null); };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: PrusaLiveZViewModel,
        dependencies: ["loginStateViewModel", "accessViewModel", "printerStateViewModel"],
        elements: ["#sidebar_plugin_prusa_livez", "#prusa_livez_control", "#prusa_livez_result"]
    });
});
