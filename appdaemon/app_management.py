import threading
import sys
import traceback
import uuid
import os
import importlib
import yaml
import subprocess
import cProfile
import io
import pstats
import logging

import appdaemon.utils as utils
from appdaemon.appdaemon import AppDaemon

class AppManagement:

    def __init__(self, ad: AppDaemon, config):

        self.AD = ad
        self.logger = ad.logging.get_child("_app_management")
        self.error = ad.logging.get_error()
        self.diag = ad.logging.get_diag()
        self.monitored_files = {}
        self.filter_files = {}
        self.modules = {}

        self.objects = {}
        self.objects_lock = threading.RLock()

        # Initialize config file tracking

        self.app_config_file_modified = 0
        self.app_config_files = {}
        self.module_dirs = []

        self.app_config_file_modified = 0
        self.app_config = {}

        self.app_config_file = config

        self.apps_initialized = False

        # Add Path for adbase

        sys.path.insert(0, os.path.dirname(__file__))

        self.process_filters()

    def terminate(self):
        self.logger.debug("terminate() called for app_management")
        if self.apps_initialized is True:
            self.check_app_updates(exit=True)

    def dump_objects(self):
        self.diag.info("--------------------------------------------------")
        self.diag.info("Objects")
        self.diag.info("--------------------------------------------------")
        with self.objects_lock:
            for object_ in self.objects.keys():
                self.diag.info("%s: %s", object_, self.objects[object_])
        self.diag.info("--------------------------------------------------")

    def get_app(self, name):
        with self.objects_lock:
            if name in self.objects:
                return self.objects[name]["object"]
            else:
                return None

    def initialize_app(self, name):
        with self.objects_lock:
            if name in self.objects:
                init = getattr(self.objects[name]["object"], "initialize", None)
                if init == None:
                    self.logger.warning("Unable to find initialize() function in module %s - skipped", name)
                    return
            else:
                self.logger.warning("Unable to find module %s - initialize() skipped", name)
                return
        # Call its initialize function

        try:
            if self.AD.threading.validate_callback_sig(name, "initialize", init):
                init()
        except:
            error_logger = logging.getLogger("Error.{}".format(name))
            error_logger.warning('-' * 60)
            error_logger.warning("Unexpected error running initialize() for %s", name)
            error_logger.warning('-' * 60)
            error_logger.warning(traceback.format_exc())
            error_logger.warning('-' * 60)
            if self.AD.logging.separate_error_log() is True:
                self.logger.warning("Logged an error to %s", self.AD.logging.get_filename(name))

    def term_object(self, name):
        with self.objects_lock:
            term = None
            if name in self.objects and hasattr(self.objects[name]["object"], "terminate"):
                self.logger.info("Calling terminate() for {}".format(name))
                # Call terminate directly rather than via worker thread
                # so we know terminate has completed before we move on

                term = self.objects[name]["object"].terminate

        if term is not None:
            try:
                term()
            except:
                error_logger = logging.getLogger("Error.{}".format(name))
                error_logger.warning('-' * 60)
                error_logger.warning("Unexpected error running terminate() for %s", name)
                error_logger.warning('-' * 60)
                error_logger.warning(traceback.format_exc())
                error_logger.warning('-' * 60)
                if self.AD.logging.separate_error_log() is True:
                    self.logger.warning("Logged an error to %s", self.AD.logging.get_filename(name))

        with self.objects_lock:
            if name in self.objects:
                del self.objects[name]

        self.AD.callbacks.clear_callbacks(name)

        self.AD.sched.term_object(name)

        if self.AD.api is not None:
            self.AD.api.term_object(name)

        # Update admin interface
        if self.AD.admin is not None and self.AD.admin.stats_update == "realtime":
            update = {"threads": self.AD.threading.get_thread_info()["threads"]}
            self.AD.appq.admin_update(update)

    def get_app_debug_level(self, app):
        with self.objects_lock:
            if app in self.objects:
                return self.AD.logging.get_level_from_int(self.objects[app]["object"].logger.getEffectiveLevel())
            else:
                return "None"

    def init_object(self, name):
        app_args = self.app_config[name]
        self.logger.info("Initializing app %s using class %s from module %s", name, app_args["class"], app_args["module"])

        if self.get_file_from_module(app_args["module"]) is not None:

            with self.objects_lock:
                if "pin_thread" in app_args:
                    if app_args["pin_thread"] < 0 or app_args["pin_thread"] >= self.AD.threading.threads:
                        self.logger.warning("pin_thread out of range ({}) in app definition for {} - app will be discarded".format(app_args["pin_thread"], name))
                        return
                    else:
                        pin = app_args["pin_thread"]
                else:
                    pin = -1

                modname = __import__(app_args["module"])
                app_class = getattr(modname, app_args["class"], None)
                if app_class is None:
                    self.logger.warning("Unable to find class %s in module %s - %s is not initialized", app_args["module"], app_args["class"], modname, name)
                else:
                    self.objects[name] = {
                        "object": app_class(
                            self.AD, name, self.AD.logging, app_args, self.AD.config, self.app_config, self.AD.global_vars
                        ),
                        "id": uuid.uuid4(),
                        "pin_app": self.AD.threading.app_should_be_pinned(name),
                        "pin_thread": pin
                    }

        else:
            self.logger.warning("Unable to find module module %s - %s is not initialized", app_args["module"], name)

    def read_config(self):

        new_config = None

        if os.path.isfile(self.app_config_file):
            self.logger.warning("apps.yaml in the Config directory is deprecated. Please move apps.yaml to the apps directory.")
            new_config = self.read_config_file(self.app_config_file)
        else:
            for root, subdirs, files in os.walk(self.AD.app_dir):
                subdirs[:] = [d for d in subdirs if d not in self.AD.exclude_dirs]
                if root[-11:] != "__pycache__":
                    for file in files:
                        if file[-5:] == ".yaml":
                            self.logger.debug("Reading %s", os.path.join(root, file))
                            config = self.read_config_file(os.path.join(root, file))
                            valid_apps = {}
                            if type(config).__name__ == "dict":
                                for app in config:
                                    if config[app] is not None:
                                        if app == "global_modules":
                                            valid_apps[app] = config[app]
                                        elif "class" in config[app] and "module" in config[app]:
                                            valid_apps[app] = config[app]
                                        else:
                                            if self.AD.invalid_yaml_warnings:
                                                self.logger.warning("App '%s' missing 'class' or 'module' entry - ignoring", app)
                            else:
                                if self.AD.invalid_yaml_warnings:
                                    self.logger.warning("File '%s' invalid structure - ignoring", os.path.join(root, file))

                            if new_config is None:
                                new_config = {}
                            for app in valid_apps:
                                if app in new_config:
                                    self.logger.warning("File '%s' duplicate app: %s - ignoring", os.path.join(root, file), app)
                                else:
                                    new_config[app] = valid_apps[app]

        return new_config

    def check_later_app_configs(self, last_latest):
        if os.path.isfile(self.app_config_file):
            ts = os.path.getmtime(self.app_config_file)
            return {"latest": ts, "files": [{"name": self.app_config_file, "ts": os.path.getmtime(self.app_config_file)}]}
        else:
            later_files = {}
            app_config_files = []
            later_files["files"] = []
            later_files["latest"] = last_latest
            later_files["deleted"] = []
            for root, subdirs, files in os.walk(self.AD.app_dir):
                subdirs[:] = [d for d in subdirs if d not in self.AD.exclude_dirs]
                if root[-11:] != "__pycache__":
                    for file in files:
                        if file[-5:] == ".yaml":
                            path = os.path.join(root, file)
                            app_config_files.append(path)
                            ts = os.path.getmtime(path)
                            if ts > last_latest:
                                later_files["files"].append(path)
                            if ts > later_files["latest"]:
                                later_files["latest"] = ts

            for file in self.app_config_files:
                if file not in app_config_files:
                    later_files["deleted"].append(file)

            if self.app_config_files != {}:
                for file in app_config_files:
                    if file not in self.app_config_files:
                        later_files["files"].append(file)

            self.app_config_files = app_config_files

            return later_files

    def read_config_file(self, file):

        new_config = None
        try:
            with open(file, 'r') as yamlfd:
                config_file_contents = yamlfd.read()

            try:
                new_config = yaml.load(config_file_contents)

            except yaml.YAMLError as exc:
                self.logger.warning("Error loading configuration")
                if hasattr(exc, 'problem_mark'):
                    if exc.context is not None:
                        self.logger.warning("parser says")
                        self.logger.warning(str(exc.problem_mark))
                        self.logger.warning(str(exc.problem) + " " + str(exc.context))
                    else:
                        self.logger.warning("parser says")
                        self.logger.warning(str(exc.problem_mark))
                        self.logger.warning(str(exc.problem))

            return new_config

        except:

            self.logger.warning('-' * 60)
            self.logger.warning("Unexpected error loading config file: %s", file)
            self.logger.warning('-' * 60)
            self.logger.warning(traceback.format_exc())
            self.logger.warning('-' * 60)

    # noinspection PyBroadException
    def check_config(self, silent=False, add_threads=True):

        terminate_apps = {}
        initialize_apps = {}
        new_config = {}
        total_apps = len(self.app_config)

        try:
            latest = self.check_later_app_configs(self.app_config_file_modified)
            self.app_config_file_modified = latest["latest"]

            if latest["files"] or latest["deleted"]:
                if silent is False:
                    self.logger.info("Reading config")
                new_config = self.read_config()
                if new_config is None:
                    if silent is False:
                        self.logger.warning("New config not applied")
                    return

                for file in latest["deleted"]:
                    if silent is False:
                        self.logger.info("%s deleted", file)

                for file in latest["files"]:
                    if silent is False:
                        self.logger.info("%s added or modified", file)

                # Check for changes

                for name in self.app_config:
                    if name in new_config:
                        if self.app_config[name] != new_config[name]:
                            # Something changed, clear and reload

                            if silent is False:
                                self.logger.info("App '%s' changed", name)
                            terminate_apps[name] = 1
                            initialize_apps[name] = 1
                    else:

                        # Section has been deleted, clear it out

                        if silent is False:
                            self.logger.info("App '{}' deleted".format(name))
                        #
                        # Since the entry has been deleted we can't sensibly determine dependencies
                        # So just immediately terminate it
                        #
                        self.term_object(name)

                for name in new_config:
                    if name not in self.app_config:
                        #
                        # New section added!
                        #
                        if "class" in new_config[name] and "module" in new_config[name]:
                            self.logger.info("App '{}' added".format(name))
                            initialize_apps[name] = 1
                        elif name == "global_modules":
                            pass
                        else:
                            if self.AD.invalid_yaml_warnings:
                                if silent is False:
                                    self.logger.warning("App '{}' missing 'class' or 'module' entry - ignoring".format(name))

                self.app_config = new_config
                total_apps = len(self.app_config)

                if silent is False:
                    self.logger.info("Running {} apps".format(total_apps))

            # Now we know if we have any new apps we can create new threads if pinning

            if add_threads is True and self.AD.threading.auto_pin is True:
                if total_apps > self.AD.threading.threads:
                    for i in range(total_apps - self.AD.threading.threads):
                        self.AD.threading.add_thread(False, True)

            return {"init": initialize_apps, "term": terminate_apps, "total": total_apps}
        except:
            self.logger.warning('-' * 60)
            self.logger.warning("Unexpected error:")
            self.logger.warning('-' * 60)
            self.logger.warning(traceback.format_exc())
            self.logger.warning('-' * 60)


    def get_app_from_file(self, file):
        module = self.get_module_from_path(file)
        for app in self.app_config:
            if "module" in self.app_config[app] and self.app_config[app]["module"] == module:
                return app
        return None

    # noinspection PyBroadException
    def read_app(self, file, reload=False):
        name = os.path.basename(file)
        module_name = os.path.splitext(name)[0]
        # Import the App
        if reload:
            self.logger.info("Reloading Module: {}".format(file))

            file, ext = os.path.splitext(name)
            #
            # Reload
            #
            try:
                importlib.reload(self.modules[module_name])
            except KeyError:
                if name not in sys.modules:
                    # Probably failed to compile on initial load
                    # so we need to re-import not reload
                    self.read_app(file)
                else:
                    # A real KeyError!
                    raise
        else:
            app = self.get_app_from_file(file)
            if app is not None:
                self.logger.info("Loading App Module: %s", file)
                if module_name not in self.modules:
                    self.modules[module_name] = importlib.import_module(module_name)
                else:
                    # We previously imported it so we need to reload to pick up any potential changes
                    importlib.reload(self.modules[module_name])

            elif "global_modules" in self.app_config and module_name in self.app_config["global_modules"]:
                self.logger.info("Loading Global Module: {}".format(file))
                self.modules[module_name] = importlib.import_module(module_name)
            else:
                if self.AD.missing_app_warnings:
                    self.logger.warning("No app description found for: %s - ignoring", file)


    @staticmethod
    def get_module_from_path(path):
        name = os.path.basename(path)
        module_name = os.path.splitext(name)[0]
        return module_name

    def get_file_from_module(self, mod):
        for file in self.monitored_files:
            module_name = self.get_module_from_path(file)
            if module_name == mod:
                return file

        return None

    def process_filters(self):
        if "filters" in self.AD.config:
            for filter in self.AD.config["filters"]:

                for root, subdirs, files in os.walk(self.AD.app_dir, topdown=True):
                    # print(root, subdirs, files)
                    #
                    # Prune dir list
                    #
                    subdirs[:] = [d for d in subdirs if d not in self.AD.exclude_dirs]

                    ext = filter["input_ext"]
                    extlen = len(ext) * -1

                    for file in files:
                        run = False
                        if file[extlen:] == ext:
                            infile = os.path.join(root, file)
                            modified = os.path.getmtime(infile)
                            if infile in self.filter_files:
                                if self.filter_files[infile] < modified:
                                    run = True
                            else:
                                self.logger.info("Found new filter file {}".format(infile))
                                run = True

                            if run is True:
                                self.logger.info("Running filter on {}".format(infile))
                                self.filter_files[infile] = modified

                                # Run the filter

                                outfile = utils.rreplace(infile, ext, filter["output_ext"], 1)
                                command_line = filter["command_line"].replace("$1", infile)
                                command_line = command_line.replace("$2", outfile)
                                try:
                                    p = subprocess.Popen(command_line, shell=True)
                                except:
                                    self.logger.warning('-' * 60)
                                    self.logger.warning("Unexpected running filter on: %s:", infile)
                                    self.logger.warning('-' * 60)
                                    self.logger.warning(traceback.format_exc())
                                    self.logger.warning('-' * 60)

    @staticmethod
    def file_in_modules(file, modules):
        for mod in modules:
            if mod["name"] == file:
                return True
        return False

    #@_timeit
    def check_app_updates(self, plugin=None, exit=False):

        if self.AD.apps is False:
            return

        # Lets add some profiling
        pr = None
        if self.AD.check_app_updates_profile is True:
            pr = cProfile.Profile()
            pr.enable()

        # Process filters

        self.process_filters()

        # Get list of apps we need to terminate and/or initialize

        apps = self.check_config()

        found_files = []
        modules = []
        for root, subdirs, files in os.walk(self.AD.app_dir, topdown=True):
            # print(root, subdirs, files)
            #
            # Prune dir list
            #
            subdirs[:] = [d for d in subdirs if d not in self.AD.exclude_dirs]

            if root[-11:] != "__pycache__":
                if root not in self.module_dirs:
                    self.logger.info("Adding %s to module import path", root)
                    sys.path.insert(0, root)
                    self.module_dirs.append(root)

            for file in files:
                if file[-3:] == ".py":
                    found_files.append(os.path.join(root, file))

        for file in found_files:
            if file == os.path.join(self.AD.app_dir, "__init__.py"):
                continue
            try:

                # check we can actually open the file

                fh = open(file)
                fh.close()

                modified = os.path.getmtime(file)
                if file in self.monitored_files:
                    if self.monitored_files[file] < modified:
                        modules.append({"name": file, "reload": True})
                        self.monitored_files[file] = modified
                else:
                    self.logger.debug("Found module %s", file)
                    modules.append({"name": file, "reload": False})
                    self. monitored_files[file] = modified
            except IOError as err:
                self.logger.warning("Unable to read app %s: %s - skipping", file, err)

        # Check for deleted modules and add them to the terminate list
        deleted_modules = []
        for file in self.monitored_files:
            if file not in found_files or exit is True:
                deleted_modules.append(file)
                self.logger.info("Removing module {}".format(file))

        for file in deleted_modules:
            del self.monitored_files[file]
            for app in self.apps_per_module(self.get_module_from_path(file)):
                apps["term"][app] = 1

        # Add any apps we need to reload because of file changes

        for module in modules:
            for app in self.apps_per_module(self.get_module_from_path(module["name"])):
                if module["reload"]:
                    apps["term"][app] = 1
                apps["init"][app] = 1

            if "global_modules" in self.app_config:
                for gm in utils.single_or_list(self.app_config["global_modules"]):
                    if gm == self.get_module_from_path(module["name"]):
                        for app in self.apps_per_global_module(gm):
                            if module["reload"]:
                                apps["term"][app] = 1
                            apps["init"][app] = 1

        if plugin is not None:
            self.logger.info("Processing restart for {}".format(plugin))
            # This is a restart of one of the plugins so check which apps need to be restarted
            for app in self.app_config:
                reload = False
                if app == "global_modules":
                    continue
                if "plugin" in self.app_config[app]:
                    for this_plugin in utils.single_or_list(self.app_config[app]["plugin"]):
                        if this_plugin == plugin:
                            # We got a match so do the reload
                            reload = True
                            break
                        elif plugin == "__ALL__":
                            reload = True
                            break
                else:
                    # No plugin dependency specified, reload to error on the side of caution
                    reload = True

                if reload is True:
                    apps["term"][app] = 1
                    apps["init"][app] = 1

        # Terminate apps

        if apps is not None and apps["term"]:

            prio_apps = self.get_app_deps_and_prios(apps["term"])

            for app in sorted(prio_apps, key=prio_apps.get, reverse=True):
                try:
                    self.logger.info("Terminating {}".format(app))
                    self.term_object(app)
                except:

                    error_logger = logging.getLogger("Error.{}".format(app))
                    error_logger.warning('-' * 60)
                    error_logger.warning("Unexpected error terminating app: %s:", app)
                    error_logger.warning('-' * 60)
                    error_logger.warning(traceback.format_exc())
                    error_logger.warning('-' * 60)
                    if self.AD.logging.separate_error_log() is True:
                        self.logger.warning("Logged an error to %s", self.AD.logging.get_filename(app))

        # Load/reload modules

        for mod in modules:
            try:
                self.read_app(mod["name"], mod["reload"])
            except:
                self.error.warning('-' * 60)
                self.error.warning("Unexpected error loading module: %s:", mod["name"])
                self.error.warning('-' * 60)
                self.error.warning(traceback.format_exc())
                self.error.warning('-' * 60)
                if self.AD.logging.separate_error_log() is True:
                    self.logger.warning("Unexpected error loading module: {}:".format(mod["name"]))

                self.logger.warning("Removing associated apps:")
                module = self.get_module_from_path(mod["name"])
                for app in self.app_config:
                    if self.app_config[app]["module"] == module:
                        if apps["init"] and app in apps["init"]:
                            del apps["init"][app]
                            self.logger.warning("{}".format(app))

        if apps is not None and apps["init"]:

            prio_apps = self.get_app_deps_and_prios(apps["init"])

            # Load Apps

            for app in sorted(prio_apps, key=prio_apps.get):
                try:
                    if "disable" in self.app_config[app] and self.app_config[app]["disable"] is True:
                        self.logger.info("{} is disabled".format(app))
                    else:
                        self.init_object(app)
                except:
                    error_logger = logging.getLogger("Error.{}".format(app))
                    error_logger.warning('-' * 60)
                    error_logger.warning("Unexpected error initializing app: %s:", app)
                    error_logger.warning('-' * 60)
                    error_logger.warning(traceback.format_exc())
                    error_logger.warning('-' * 60)
                    if self.AD.logging.separate_error_log() is True:
                        self.logger.warning("Logged an error to %s", self.AD.logging.get_filename(app))

            self.AD.threading.calculate_pin_threads()

            # Call initialize() for apps

            for app in sorted(prio_apps, key=prio_apps.get):
                if "disable" in self.app_config[app] and self.app_config[app]["disable"] is True:
                    pass
                else:
                    self.initialize_app(app)

        if self.AD.check_app_updates_profile is True:
            pr.disable()

        s = io.StringIO()
        sortby = 'cumulative'
        ps = pstats.Stats(pr, stream=s).sort_stats(sortby)
        ps.print_stats()
        self.check_app_updates_profile_stats = s.getvalue()

        self.apps_initialized = True

    def get_app_deps_and_prios(self, applist):

        # Build a list of modules and their dependencies

        deplist = []
        for app in applist:
            if app not in deplist:
                deplist.append(app)
            self.get_dependent_apps(app, deplist)

        # Need to gove the topological sort a full list of apps or it will fail
        full_list = list(self.app_config.keys())

        deps = []

        for app in full_list:
            dependees = []
            if "dependencies" in self.app_config[app]:
                for dep in utils.single_or_list(self.app_config[app]["dependencies"]):
                    if dep in self.app_config:
                        dependees.append(dep)
                    else:
                        self.logger.warning("Unable to find app {} in dependencies for {}".format(dep, app))
                        self.logger.warning("Ignoring app {}".format(app))
            deps.append((app, dependees))

        prio_apps = {}
        prio = float(50.1)
        try:
            for app in self.topological_sort(deps):
                if "dependencies" in self.app_config[app] or self.app_has_dependents(app):
                    prio_apps[app] = prio
                    prio += float(0.0001)
                else:
                    if "priority" in self.app_config[app]:
                        prio_apps[app] = float(self.app_config[app]["priority"])
                    else:
                        prio_apps[app] = float(50)
        except ValueError:
            pass

        # now we remove the ones we aren't interested in

        final_apps = {}
        for app in prio_apps:
            if app in deplist:
                final_apps[app] = prio_apps[app]

        return final_apps

    def app_has_dependents(self, name):
        for app in self.app_config:
            if "dependencies" in self.app_config[app]:
                for dep in utils.single_or_list(self.app_config[app]["dependencies"]):
                    if dep == name:
                        return True
        return False

    def get_dependent_apps(self, dependee, deps):
        for app in self.app_config:
            if "dependencies" in self.app_config[app]:
                for dep in utils.single_or_list(self.app_config[app]["dependencies"]):
                    #print("app= {} dep = {}, dependee = {} deps = {}".format(app, dep, dependee, deps))
                    if dep == dependee and app not in deps:
                        deps.append(app)
                        new_deps = self.get_dependent_apps(app, deps)
                        if new_deps is not None:
                            deps.append(new_deps)

    def topological_sort(self, source):

        pending = [(name, set(deps)) for name, deps in source]  # copy deps so we can modify set in-place
        emitted = []
        while pending:
            next_pending = []
            next_emitted = []
            for entry in pending:
                name, deps = entry
                deps.difference_update(emitted)  # remove deps we emitted last pass
                if deps:  # still has deps? recheck during next pass
                    next_pending.append(entry)
                else:  # no more deps? time to emit
                    yield name
                    emitted.append(name)  # <-- not required, but helps preserve original ordering
                    next_emitted.append(name)  # remember what we emitted for difference_update() in next pass
            if not next_emitted:
                # all entries have unmet deps, we have cyclic redundancies
                # since we already know all deps are correct
                self.logger.warning("Cyclic or missing app dependencies detected")
                for pend in next_pending:
                    deps = ""
                    for dep in pend[1]:
                        deps += "{} ".format(dep)
                    self.logger.warning("{} depends on {}".format(pend[0], deps))
                raise ValueError("cyclic dependancy detected")
            pending = next_pending
            emitted = next_emitted

    def apps_per_module(self, module):
        apps = []
        for app in self.app_config:
            if app != "global_modules" and self.app_config[app]["module"] == module:
                apps.append(app)

        return apps

    def apps_per_global_module(self, module):
        apps = []
        for app in self.app_config:
            if "global_dependencies" in self.app_config[app]:
                for gm in utils.single_or_list(self.app_config[app]["global_dependencies"]):
                    if gm == module:
                        apps.append(app)

        return apps