# coding=utf-8
from __future__ import absolute_import

import octoprint.plugin
import flask, json
from io import BytesIO
from octoprint.server.util.flask import restricted_access
from octoprint.events import eventManager, Events
from octoprint.access.permissions import Permissions,ADMIN_GROUP,USER_GROUP
import octoprint.filemanager
from octoprint.filemanager.util import StreamWrapper
from octoprint.filemanager.destinations import FileDestinations

from .print_queue import PrintQueue, QueueItem
from .driver import ContinuousPrintDriver


QUEUE_KEY = "cp_queue"
CLEARING_SCRIPT_KEY = "cp_bed_clearing_script"
FINISHED_SCRIPT_KEY = "cp_queue_finished_script"
TEMP_FILES = dict([(k, f"{k}.gcode") for k in [FINISHED_SCRIPT_KEY, CLEARING_SCRIPT_KEY]])
RESTART_MAX_RETRIES_KEY = "cp_restart_on_pause_max_restarts"
RESTART_ON_PAUSE_KEY = "cp_restart_on_pause_enabled"
RESTART_MAX_TIME_KEY = "cp_restart_on_pause_max_seconds"

class ContinuousprintPlugin(
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.BlueprintPlugin,
    octoprint.plugin.EventHandlerPlugin,
):

    def _msg(self, msg="", type="popup"):
        self._plugin_manager.send_plugin_message(
            self._identifier, dict(type=type, msg=msg)
        )


    def _update_driver_settings(self):
        self.d.set_retry_on_pause(
            self._settings.get([RESTART_ON_PAUSE_KEY]),
            self._settings.get([RESTART_MAX_RETRIES_KEY]),
            self._settings.get([RESTART_MAX_TIME_KEY]),
        )

    ##~~ SettingsPlugin
    def get_settings_defaults(self):
        d = {}
        d[QUEUE_KEY] = "[]"
        d[CLEARING_SCRIPT_KEY] = (
            "M17 ;enable steppers\n"
            "G91 ; Set relative for lift\n"
            "G0 Z10 ; lift z by 10\n"
            "G90 ;back to absolute positioning\n"
            "M190 R25 ; set bed to 25 and wait for cooldown\n"
            "G0 X200 Y235 ;move to back corner\n"
            "G0 X110 Y235 ;move to mid bed aft\n"
            "G0 Z1 ;come down to 1MM from bed\n"
            "G0 Y0 ;wipe forward\n"
            "G0 Y235 ;wipe aft\n"
            "G28 ; home"
        )
        d[FINISHED_SCRIPT_KEY] = (
            "M18 ; disable steppers\n"
            "M104 T0 S0 ; extruder heater off\n"
            "M140 S0 ; heated bed heater off\n"
            "M300 S880 P300 ; beep to show its finished"
        )
        d[RESTART_MAX_RETRIES_KEY] = 3
        d[RESTART_ON_PAUSE_KEY] = False
        d[RESTART_MAX_TIME_KEY] = 60*60
        return d


    def _rm_temp_files(self):
        # Clean up any file references from prior runs
        for path in TEMP_FILES.values():
          if self._file_manager.file_exists(FileDestinations.LOCAL, path):
            self._file_manager.remove_file(FileDestinations.LOCAL, path)

    ##~~ StartupPlugin
    def on_after_startup(self):
        self._settings.save()
        self.q = PrintQueue(self._settings, QUEUE_KEY)
        self.d = ContinuousPrintDriver(
                    queue = self.q,
                    finish_script_fn = self.run_finish_script,
                    clear_bed_fn = self.clear_bed,
                    start_print_fn = self.start_print,
                    cancel_print_fn = self.cancel_print,
                    logger = self._logger,
                )
        self._update_driver_settings()
        self._rm_temp_files()
        self._logger.info("Continuous Print Plugin started")

    ##~~ EventHandlerPlugin
    def on_event(self, event, payload):
        if not hasattr(self, "d"): # Ignore any messages arriving before init
            return
        
        is_current_path = payload is not None and payload.get('path') == self.d.current_path()
        is_finish_script = payload is not None and payload.get('path') == TEMP_FILES[FINISHED_SCRIPT_KEY]

        if event == Events.METADATA_ANALYSIS_FINISHED:
            # OctoPrint analysis writes to the printing file - we must remove
            # our temp files AFTER analysis has finished or else we'll get a "file not found" log error.
            # We do so when either we've finished printing or when the temp file is no longer selected
            if self._printer.get_state_id() != "OPERATIONAL":
                for path in TEMP_FILES.values():
                    if self._printer.is_current_file(path, sd=False):
                        return
            self._rm_temp_files()
        elif (is_current_path or is_finish_script) and event == Events.PRINT_DONE:
            self.d.on_print_success(is_finish_script)
            self.paused = False
            self._msg(type="reload") # reload UI
        elif is_current_path and event == Events.PRINT_FAILED and payload["reason"] != "cancelled":
            # Note that cancelled events are already handled directly with Events.PRINT_CANCELLED
            self.d.on_print_failed()
            self.paused = False
            self._msg(type="reload") # reload UI
        elif is_current_path and event == Events.PRINT_CANCELLED:
            self.d.on_print_cancelled()
            self.paused = False
            self._msg(type="reload") # reload UI
        elif is_current_path and event == Events.PRINT_PAUSED:
            self.d.on_print_paused(is_temp_file=(payload['path'] in TEMP_FILES.values()))
            self.paused = True
            self._msg(type="reload") # reload UI
        elif is_current_path and event == Events.PRINT_RESUMED:
            self.d.on_print_resumed()
            self.paused = False
            self._msg(type="reload")
        elif event == Events.PRINTER_STATE_CHANGED and self._printer.get_state_id() == "OPERATIONAL":
            self._msg(type="reload") # reload UI
        elif event == Events.UPDATED_FILES:
            self._msg(type="updatefiles")
        elif event == Events.SETTINGS_UPDATED:
            self._update_driver_settings()
        # Play out actions until printer no longer in a state where we can run commands
        # Note that PAUSED state is respected so that gcode can include `@pause` commands.
        # See https://docs.octoprint.org/en/master/features/atcommands.html
        while self._printer.get_state_id() == "OPERATIONAL" and self.d.pending_actions() > 0:
            self._logger.warning("on_printer_ready")
            self.d.on_printer_ready()

    def _write_temp_gcode(self, key):
        gcode = self._settings.get([key])
        file_wrapper = StreamWrapper(key, BytesIO(gcode.encode("utf-8")))
        added_file = self._file_manager.add_file(
                octoprint.filemanager.FileDestinations.LOCAL,
                TEMP_FILES[key],
                file_wrapper,
                allow_overwrite=True,
        )
        self._logger.info(f"Wrote file {added_file}")
        return added_file

    def run_finish_script(self):
        self._msg("Print Queue Complete", type="complete")
        path = self._write_temp_gcode(FINISHED_SCRIPT_KEY)
        self._printer.select_file(path, sd=False, printAfterSelect=True)  

    def cancel_print(self):
        self._msg("Print cancelled", type="error")
        self._printer.cancel_print()

    def clear_bed(self):
        path = self._write_temp_gcode(CLEARING_SCRIPT_KEY)
        self._printer.select_file(path, sd=False, printAfterSelect=True)

    def start_print(self, item, clear_bed=True):
        self._msg("Starting print: " + item.name)
        self._msg(type="reload")
        try:
            self._printer.select_file(item.path, item.sd)
            self._logger.info(item.path)
            self._printer.start_print()
        except InvalidFileLocation:
            self._msg("File not found: " + item.path, type="error")
        except InvalidFileType:
            self._msg("File not gcode: " + item.path, type="error")

    def state_json(self, changed=None):
        # Values are stored serialized, so we need to create a json string and inject them
        q = self._settings.get([QUEUE_KEY])
        if changed is not None:
            q = json.loads(q)
            for i in changed:
                if i<len(q):# no deletion of last item
                    q[i]["changed"] = True
            q = json.dumps(q)
    
        resp = ('{"active": %s, "status": "%s", "queue": %s}' % (
                "true" if hasattr(self, "d") and self.d.active else "false",
                "Initializing" if not hasattr(self, "d") else self.d.status,
                q
            ))
        return resp
            
    # Listen for resume from printer ("M118 //action:queuego"), only act if actually paused. #from @grtrenchman
    def resume_action_handler(self, comm, line, action, *args, **kwargs):
        if not action == "queuego":
            return
        if self.paused:
            self.d.set_active()
        
    ##~~ APIs
    @octoprint.plugin.BlueprintPlugin.route("/state", methods=["GET"])
    @restricted_access
    def state(self):
        return self.state_json()

    @octoprint.plugin.BlueprintPlugin.route("/move", methods=["POST"])
    @restricted_access
    def move(self):
        if not Permissions.PLUGIN_CONTINUOUSPRINT_CHQUEUE.can():
            return flask.make_response("Insufficient Rights", 403)
            self._logger.info("attempt failed due to insufficient permissions.")
        idx = int(flask.request.form["idx"])
        count = int(flask.request.form["count"])
        offs = int(flask.request.form["offs"])
        self.q.move(idx, count, offs)
        return self.state_json(changed=range(idx+offs, idx+offs+count))

    @octoprint.plugin.BlueprintPlugin.route("/assign", methods=["POST"])
    @restricted_access
    def assign(self):
        if not Permissions.PLUGIN_CONTINUOUSPRINT_ASSIGNQUEUE.can():
            return flask.make_response("Insufficient Rights", 403)
            self._logger.info("attempt failed due to insufficient permissions.")
        items = json.loads(flask.request.form["items"])
        self.q.assign([QueueItem(
                name=i["name"],
                path=i["path"],
                sd=i["sd"],
                job=i["job"],
                run=i["run"],
                start_ts=i.get("start_ts"),
                end_ts=i.get("end_ts"),
                result=i.get("result"),
                retries=i.get("retries"),
            ) for i in items])
        return self.state_json(changed=[])

    @octoprint.plugin.BlueprintPlugin.route("/add", methods=["POST"])
    @restricted_access
    def add(self):
        if not Permissions.PLUGIN_CONTINUOUSPRINT_ADDQUEUE.can():
            return flask.make_response("Insufficient Rights", 403)
            self._logger.info("attempt failed due to insufficient permissions.")
        idx = flask.request.form.get("idx")
        if idx is None:
            idx = len(self.q)
        else:
            idx = int(idx)
        items = json.loads(flask.request.form["items"])
        self.q.add([QueueItem(
                name=i["name"],
                path=i["path"],
                sd=i["sd"],
                job=i["job"],
                run=i["run"],
            ) for i in items], idx)
        return self.state_json(changed=range(idx, idx+len(items)))

    @octoprint.plugin.BlueprintPlugin.route("/remove", methods=["POST"])
    @restricted_access
    def remove(self):
        if not Permissions.PLUGIN_CONTINUOUSPRINT_RMQUEUE.can():
            return flask.make_response("Insufficient Rights", 403)
            self._logger.info("attempt failed due to insufficient permissions.")
        idx = int(flask.request.form["idx"])
        count = int(flask.request.form["count"])
        self.q.remove(idx, count)
        return self.state_json(changed=[idx])
        
    @octoprint.plugin.BlueprintPlugin.route("/set_active", methods=["POST"])
    @restricted_access
    def set_active(self):
        if not Permissions.PLUGIN_CONTINUOUSPRINT_STARTQUEUE.can():
            return flask.make_response("Insufficient Rights", 403)
            self._logger.info(f"attempt failed due to insufficient permissions.")
        self.d.set_active(flask.request.form["active"] == "true", printer_ready=(self._printer.get_state_id() == "OPERATIONAL"))
        return self.state_json()

    @octoprint.plugin.BlueprintPlugin.route("/clear", methods=["POST"])
    @restricted_access
    def clear(self):
        i = 0
        keep_failures = (flask.request.form["keep_failures"] == "true")
        keep_non_ended = (flask.request.form["keep_non_ended"] == "true")
        self._logger.info(f"Clearing queue (keep_failures={keep_failures}, keep_non_ended={keep_non_ended})")
        changed = []
        while i < len(self.q):
            v = self.q[i]
            self._logger.info(f"{v.name} -- end_ts {v.end_ts} result {v.result}")
            if v.end_ts is None and keep_non_ended:
                i = i + 1
            elif v.result == "failure" and keep_failures:
                i = i + 1
            else:
                del self.q[i]
                changed.append(i)
        return self.state_json(changed=changed)

    @octoprint.plugin.BlueprintPlugin.route("/reset", methods=["POST"])
    @restricted_access
    def reset(self):
        idxs = json.loads(flask.request.form["idxs"])
        for idx in idxs:
            i = self.q[idx]
            i.start_ts = None
            i.end_ts = None
        self.q.remove(idx, count)
        return self.state_json(changed=[idx])

    ##~~  TemplatePlugin
    def get_template_vars(self):
        return dict(
            cp_enabled=(self.d.active if hasattr(self, "d") else False),
            cp_bed_clearing_script=self._settings.get([CLEARING_SCRIPT_KEY]),
            cp_queue_finished=self._settings.get([FINISHED_SCRIPT_KEY]),
            cp_restart_on_pause_enabled=self._settings.get_boolean([RESTART_ON_PAUSE_KEY]),
            cp_restart_on_pause_max_seconds=self._settings.get_int([RESTART_MAX_TIME_KEY]),
            cp_restart_on_pause_max_restarts=self._settings.get_int([RESTART_MAX_RETRIES_KEY]),
        )

    def get_template_configs(self):
        return [
            dict(
                type="settings",
                custom_bindings=False,
                template="continuousprint_settings.jinja2",
            ),
            dict(
                type="tab", custom_bindings=False, template="continuousprint_tab.jinja2"
            ),
        ]

    ##~~ AssetPlugin
    def get_assets(self):
        return dict(js=[
            "js/continuousprint_api.js",
            "js/continuousprint_queueitem.js",
            "js/continuousprint_queueset.js",
            "js/continuousprint_job.js",
            "js/continuousprint_viewmodel.js",
            "js/continuousprint.js",
            "js/sortable.js",
            "js/knockout-sortable.js",
            ], css=["css/continuousprint.css"])

    def get_update_information(self):
        # Define the configuration for your plugin to use with the Software Update
        # Plugin here. See https://docs.octoprint.org/en/master/bundledplugins/softwareupdate.html
        # for details.
        return dict(
            continuousprint=dict(
                displayName="Automation Queue Plugin",
                displayVersion=self._plugin_version,
                # version check: github repository
                type="github_release",
                user="smartin015",
                repo="continuousprint",
                current=self._plugin_version,
                stable_branch=dict(
                    name="Stable", branch="stable", comittish=["stable"]
                ),
                prerelease_branches=[
                    dict(
                        name="Release Candidate",
                        branch="rc",
                        comittish=["rc", "master"],
                    )
                ],
                # update method: pip
                pip="https://github.com/smartin015/continuousprint/archive/{target_version}.zip",
            )
        )
    def add_permissions(*args, **kwargs):
        return [
            dict(key="STARTQUEUE",
                name="Start Queue",
                description="Allows for starting queue",
                roles=["admin","continuousprint-start"],
                dangerous=True,
                default_groups=[ADMIN_GROUP]
            ),
            dict(key="ADDQUEUE",
                name="Add to Queue",
                description="Allows for adding prints to the queue",
                roles=["admin","continuousprint-add"],
                dangerous=True,
                default_groups=[ADMIN_GROUP]
            ),
            dict(key="RMQUEUE",
                name="Remove Print from Queue ",
                description="Allows for removing prints from the queue",
                roles=["admin","continuousprint-remove"],
                dangerous=True,
                default_groups=[ADMIN_GROUP]
            ),
            dict(key="CHQUEUE",
                name="Move items in Queue ",
                description="Allows for moving items in the queue",
                roles=["admin","continuousprint-move"],
                dangerous=True,
                default_groups=[ADMIN_GROUP]
            ),
            dict(key="ASSIGNQUEUE",
                name="Assign the whole Queue",
                description="Allows for loading the whole queue from JSON",
                roles=["admin","continuousprint-assign"],
                dangerous=True,
                default_groups=[ADMIN_GROUP]
            ),
        ]

__plugin_name__ = "Automation Queue"
__plugin_pythoncompat__ = ">=3.6,<4"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = ContinuousprintPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
        "octoprint.access.permissions": __plugin_implementation__.add_permissions,
        "octoprint.comm.protocol.action": __plugin_implementation__.resume_action_handler # register to listen for "M118 //action:" commands
    }
