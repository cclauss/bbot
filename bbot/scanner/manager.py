import asyncio
import logging
import traceback
from contextlib import suppress

from ..errors import ValidationError
from ..core.helpers.async_helpers import TaskCounter, ShuffleQueue

log = logging.getLogger("bbot.scanner.manager")


class ScanManager:
    """
    Manages the modules, event queues, and overall event flow during a scan.

    Simultaneously serves as a policeman, judge, jury, and executioner for events.
    It is responsible for managing the incoming event queue and distributing events to modules.

    Attributes:
        scan (Scan): Reference to the Scan object that instantiated the ScanManager.
        incoming_event_queue (ShuffleQueue): Queue storing incoming events for processing.
        events_distributed (set): Set tracking globally unique events.
        events_accepted (set): Set tracking events accepted by individual modules.
        dns_resolution (bool): Flag to enable or disable DNS resolution.
        _task_counter (TaskCounter): Counter for ongoing tasks.
        _new_activity (bool): Flag indicating new activity.
        _modules_by_priority (dict): Modules sorted by their priorities.
        _incoming_queues (list): List of incoming event queues from each module.
        _module_priority_weights (list): Weight values for each module based on priority.
    """

    def __init__(self, scan):
        """
        Initializes the ScanManager object, setting up essential attributes for scan management.

        Args:
            scan (Scan): Reference to the Scan object that instantiated the ScanManager.
        """

        self.scan = scan
        self.preset = scan.preset

        self.incoming_event_queue = ShuffleQueue()
        # track incoming duplicates module-by-module (for `suppress_dupes` attribute of modules)
        self.incoming_dup_tracker = set()
        # track outgoing duplicates (for `accept_dupes` attribute of modules)
        self.outgoing_dup_tracker = set()
        self.dns_resolution = self.scan.config.get("dns_resolution", False)
        self._task_counter = TaskCounter()
        self._new_activity = True
        self._modules_by_priority = None
        self._hook_modules = None
        self._non_hook_modules = None
        self._incoming_queues = None
        self._module_priority_weights = None

    async def _worker_loop(self):
        try:
            while not self.scan.stopped:
                try:
                    async with self._task_counter.count("get_event_from_modules()"):
                        # if we have hooks set up, we always get events from the last (lowest priority) hook module.
                        if self.hook_modules:
                            last_hook_module = self.hook_modules[-1]
                            event, kwargs = last_hook_module.outgoing_event_queue.get_nowait()
                        else:
                            # otherwise, we go through all the modules
                            event, kwargs = self.get_event_from_modules()
                except asyncio.queues.QueueEmpty:
                    await asyncio.sleep(0.1)
                    continue
                async with self._task_counter.count(f"emit_event({event})"):
                    emit_event_task = asyncio.create_task(
                        self.emit_event(event, **kwargs), name=f"emit_event({event})"
                    )
                    await emit_event_task

        except Exception:
            log.critical(traceback.format_exc())

    async def init_events(self):
        """
        Initializes events by seeding the scanner with target events and distributing them for further processing.

        Notes:
            - This method populates the event queue with initial target events.
            - It also marks the Scan object as finished with initialization by setting `_finished_init` to True.
        """

        context = f"manager.init_events()"
        async with self.scan._acatch(context), self._task_counter.count(context):

            sorted_events = sorted(self.scan.target.events, key=lambda e: len(e.data))
            for event in [self.scan.root_event] + sorted_events:
                event._dummy = False
                event.scope_distance = 0
                event.web_spider_distance = 0
                event.scan = self.scan
                if event.source is None:
                    event.source = self.scan.root_event
                if event.module is None:
                    event.module = self.scan._make_dummy_module(name="TARGET", _type="TARGET")
                self.scan.verbose(f"Target: {event}")
                if self.hook_modules:
                    first_hook_module = self.hook_modules[0]
                    await first_hook_module.queue_event(event)
                else:
                    self.queue_event(event)
            await asyncio.sleep(0.1)
            self.scan._finished_init = True

    async def emit_event(self, event, *args, **kwargs):
        """
        TODO: Register + kill duplicate events immediately?
        bbot.scanner: scan._event_thread_pool: running for 0 seconds: ScanManager._emit_event(DNS_NAME("sipfed.online.lync.com"))
        bbot.scanner: scan._event_thread_pool: running for 0 seconds: ScanManager._emit_event(DNS_NAME("sipfed.online.lync.com"))
        bbot.scanner: scan._event_thread_pool: running for 0 seconds: ScanManager._emit_event(DNS_NAME("sipfed.online.lync.com"))
        """
        callbacks = ["abort_if", "on_success_callback"]
        callbacks_requested = any([kwargs.get(k, None) is not None for k in callbacks])
        # "quick" queues the event immediately
        # This is used by speculate
        quick_kwarg = kwargs.pop("quick", False)
        quick_event = getattr(event, "quick_emit", False)
        quick = (quick_kwarg or quick_event) and not callbacks_requested

        # skip event if it fails precheck
        acceptable = self._event_precheck(event)
        if not acceptable:
            return

        log.debug(f'Module "{event.module}" raised {event}')

        if quick:
            log.debug(f"Quick-emitting {event}")
            for kwarg in callbacks:
                kwargs.pop(kwarg, None)
            async with self.scan._acatch(context=self.distribute_event):
                await self.distribute_event(event)
        else:
            async with self.scan._acatch(context=self._emit_event):
                await self._emit_event(
                    event,
                    *args,
                    **kwargs,
                )

    def _event_precheck(self, event):
        """
        Check an event to see if we can skip it to save on performance
        """
        if event._dummy:
            log.warning(f"Cannot emit dummy event: {event}")
            return False
        if event == event.get_source():
            log.debug(f"Skipping event with self as source: {event}")
            return False
        if event._graph_important:
            return True
        if self.is_incoming_duplicate(event, add=True):
            log.debug(f"Skipping event because it was already emitted by its module: {event}")
            return False
        return True

    async def _emit_event(self, event, **kwargs):
        """
        Handles the emission, tagging, and distribution of a events during a scan.

        A lot of really important stuff happens here. Actually this is probably the most
        important method in all of BBOT. It is basically the central intersection that
        every event passes through.

        It exists in a delicate balance. Close to half of my debugging time has been spent
        in this function. I have slain many dragons here and there may still be more yet to slay.

        Tread carefully, friend. -TheTechromancer

        Notes:
            - Central function for decision-making in BBOT.
            - Conducts DNS resolution, tagging, and scope calculations.
            - Checks against whitelists and blacklists.
            - Calls custom callbacks.
            - Handles DNS wildcard events.
            - Decides on event acceptance and distribution.

        Parameters:
            event (Event): The event object to be emitted.
            **kwargs: Arbitrary keyword arguments (e.g., `on_success_callback`, `abort_if`).

        Side Effects:
            - Event tagging.
            - Populating DNS data.
            - Emitting new events.
            - Queueing events for further processing.
            - Adjusting event scopes.
            - Running callbacks.
            - Updating scan statistics.
        """
        log.debug(f"Emitting {event}")
        try:
            on_success_callback = kwargs.pop("on_success_callback", None)
            abort_if = kwargs.pop("abort_if", None)

            # blacklist rejections
            event_blacklisted = self.scan.blacklisted(event)
            if event_blacklisted or "blacklisted" in event.tags:
                log.debug(f"Omitting blacklisted event: {event}")
                return

            # Scope shepherding
            # here is where we make sure in-scope events are set to their proper scope distance
            event_whitelisted = self.scan.whitelisted(event)
            if event.host and event_whitelisted:
                log.debug(f"Making {event} in-scope because it matches the scan target")
                event.scope_distance = 0

            # now that the event is properly tagged, we can finally make decisions about it
            abort_result = False
            if callable(abort_if):
                async with self.scan._acatch(context=abort_if):
                    abort_result = await self.scan.helpers.execute_sync_or_async(abort_if, event)
                msg = f"{event.module}: not raising event {event} due to custom criteria in abort_if()"
                with suppress(ValueError, TypeError):
                    abort_result, reason = abort_result
                    msg += f": {reason}"
                if abort_result:
                    log.verbose(msg)
                    return

            # run success callback before distributing event (so it can add tags, etc.)
            if callable(on_success_callback):
                async with self.scan._acatch(context=on_success_callback):
                    await self.scan.helpers.execute_sync_or_async(on_success_callback, event)

            await self.distribute_event(event)

        except ValidationError as e:
            log.warning(f"Event validation failed with kwargs={kwargs}: {e}")
            log.trace(traceback.format_exc())

        finally:
            log.debug(f"{event.module}.emit_event() finished for {event}")

    def is_incoming_duplicate(self, event, add=False):
        """
        Calculate whether an event is a duplicate in the context of the module that emitted it
        This will return True if the event's parent module has raised the event before.
        """
        try:
            event_hash = event.module._outgoing_dedup_hash(event)
        except AttributeError:
            event_hash = hash((event, str(getattr(event, "module", ""))))
        is_dup = event_hash in self.incoming_dup_tracker
        if add:
            self.incoming_dup_tracker.add(event_hash)
        suppress_dupes = getattr(event.module, "suppress_dupes", True)
        if suppress_dupes and is_dup:
            return True
        return False

    def is_outgoing_duplicate(self, event, add=False):
        """
        Calculate whether an event is a duplicate in the context of the whole scan,
        This will return True if the same event (irregardless of its source module) has been emitted before.

        TODO: Allow modules to use this for custom deduplication such as on a per-host or per-domain basis.
        """
        event_hash = hash(event)
        is_dup = event_hash in self.outgoing_dup_tracker
        if add:
            self.outgoing_dup_tracker.add(event_hash)
        return is_dup

    async def distribute_event(self, event):
        """
        Queue event with modules
        """
        async with self.scan._acatch(context=self.distribute_event):
            # make event internal if it's above our configured report distance
            event_in_report_distance = event.scope_distance <= self.scan.scope_report_distance
            event_will_be_output = event.always_emit or event_in_report_distance
            if not event_will_be_output:
                log.debug(
                    f"Making {event} internal because its scope_distance ({event.scope_distance}) > scope_report_distance ({self.scan.scope_report_distance})"
                )
                event.internal = True

            # if we discovered something interesting from an internal event,
            # make sure we preserve its chain of parents
            source = event.source
            if source.internal and ((not event.internal) or event._graph_important):
                source_in_report_distance = source.scope_distance <= self.scan.scope_report_distance
                if source_in_report_distance:
                    source.internal = False
                if not source._graph_important:
                    source._graph_important = True
                    log.debug(f"Re-queuing internal event {source} with parent {event}")
                    self.queue_event(source)

            is_outgoing_duplicate = self.is_outgoing_duplicate(event)
            if is_outgoing_duplicate:
                self.scan.verbose(f"{event.module}: Duplicate event: {event}")
            # absorb event into the word cloud if it's in scope
            if not is_outgoing_duplicate and -1 < event.scope_distance < 1:
                self.scan.word_cloud.absorb_event(event)
            for mod in self.scan.modules.values():
                # don't distribute events to hook modules
                if mod._hook:
                    continue
                acceptable_dup = (not is_outgoing_duplicate) or mod.accept_dupes
                # graph_important = mod._type == "output" and event._graph_important == True
                graph_important = mod._is_graph_important(event)
                if acceptable_dup or graph_important:
                    await mod.queue_event(event)

    def kill_module(self, module_name, message=None):
        from signal import SIGINT

        module = self.scan.modules[module_name]
        module.set_error_state(message=message, clear_outgoing_queue=True)
        for proc in module._proc_tracker:
            with suppress(Exception):
                proc.send_signal(SIGINT)
        self.scan.helpers.cancel_tasks_sync(module._tasks)

    @property
    def modules_by_priority(self):
        if not self._modules_by_priority:
            self._modules_by_priority = sorted(list(self.scan.modules.values()), key=lambda m: m.priority)
        return self._modules_by_priority

    @property
    def incoming_queues(self):
        if not self._incoming_queues:
            queues_by_priority = [m.outgoing_event_queue for m in self.modules_by_priority if not m._hook]
            self._incoming_queues = [self.incoming_event_queue] + queues_by_priority
        return self._incoming_queues

    @property
    def incoming_qsize(self):
        incoming_events = 0
        for q in self.incoming_queues:
            incoming_events += q.qsize()
        return incoming_events

    @property
    def module_priority_weights(self):
        if not self._module_priority_weights:
            # we subtract from six because lower priorities == higher weights
            priorities = [5] + [6 - m.priority for m in self.modules_by_priority if not m._hook]
            self._module_priority_weights = priorities
        return self._module_priority_weights

    @property
    def hook_modules(self):
        if self._hook_modules is None:
            self._hook_modules = [m for m in self.modules_by_priority if m._hook]
            if self._hook_modules:
                self._hook_modules[0]._first = True
        return self._hook_modules

    @property
    def non_hook_modules(self):
        if self._non_hook_modules is None:
            self._non_hook_modules = [m for m in self.modules_by_priority if not m._hook]
        return self._non_hook_modules

    def get_event_from_modules(self):
        for q in self.scan.helpers.weighted_shuffle(self.incoming_queues, self.module_priority_weights):
            try:
                return q.get_nowait()
            except (asyncio.queues.QueueEmpty, AttributeError):
                continue
        raise asyncio.queues.QueueEmpty()

    @property
    def queued_event_types(self):
        event_types = {}
        for q in self.incoming_queues:
            for event, _ in q._queue:
                event_type = getattr(event, "type", None)
                if event_type is not None:
                    try:
                        event_types[event_type] += 1
                    except KeyError:
                        event_types[event_type] = 1
        return event_types

    def queue_event(self, event, **kwargs):
        if event:
            # nerf event's priority if it's likely not to be in scope
            if event.scope_distance > 0:
                event_in_scope = self.scan.whitelisted(event) and not self.scan.blacklisted(event)
                if not event_in_scope:
                    event.module_priority += event.scope_distance
            # update event's scope distance based on its parent
            event.scope_distance = event.source.scope_distance + 1
            self.incoming_event_queue.put_nowait((event, kwargs))

    @property
    def running(self):
        active_tasks = self._task_counter.value
        incoming_events = self.incoming_qsize
        return active_tasks > 0 or incoming_events > 0

    @property
    def modules_finished(self):
        finished_modules = [m.finished for m in self.scan.modules.values()]
        return all(finished_modules)

    @property
    def active(self):
        return self.running or not self.modules_finished

    def modules_status(self, _log=False):
        finished = True
        status = {"modules": {}}

        for m in self.scan.modules.values():
            mod_status = m.status
            if mod_status["running"]:
                finished = False
            status["modules"][m.name] = mod_status

        for mod in self.scan.modules.values():
            if mod.errored and mod.incoming_event_queue not in [None, False]:
                with suppress(Exception):
                    mod.set_error_state()

        status["finished"] = finished

        modules_errored = [m for m, s in status["modules"].items() if s["errored"]]

        max_mem_percent = 90
        mem_status = self.scan.helpers.memory_status()
        # abort if we don't have the memory
        mem_percent = mem_status.percent
        if mem_percent > max_mem_percent:
            free_memory = mem_status.available
            free_memory_human = self.scan.helpers.bytes_to_human(free_memory)
            self.scan.warning(f"System memory is at {mem_percent:.1f}% ({free_memory_human} remaining)")

        if _log:
            modules_status = []
            for m, s in status["modules"].items():
                running = s["running"]
                incoming = s["events"]["incoming"]
                outgoing = s["events"]["outgoing"]
                tasks = s["tasks"]
                total = sum([incoming, outgoing, tasks])
                if running or total > 0:
                    modules_status.append((m, running, incoming, outgoing, tasks, total))
            modules_status.sort(key=lambda x: x[-1], reverse=True)

            if modules_status:
                modules_status_str = ", ".join([f"{m}({i:,}:{t:,}:{o:,})" for m, r, i, o, t, _ in modules_status])
                self.scan.info(
                    f"{self.scan.name}: Modules running (incoming:processing:outgoing) {modules_status_str}"
                )
            else:
                self.scan.info(f"{self.scan.name}: No modules running")
            event_type_summary = sorted(
                self.scan.stats.events_emitted_by_type.items(), key=lambda x: x[-1], reverse=True
            )
            if event_type_summary:
                self.scan.info(
                    f'{self.scan.name}: Events produced so far: {", ".join([f"{k}: {v}" for k,v in event_type_summary])}'
                )
            else:
                self.scan.info(f"{self.scan.name}: No events produced yet")

            if modules_errored:
                self.scan.verbose(
                    f'{self.scan.name}: Modules errored: {len(modules_errored):,} ({", ".join([m for m in modules_errored])})'
                )

            queued_events_by_type = [(k, v) for k, v in self.queued_event_types.items() if v > 0]
            if queued_events_by_type:
                queued_events_by_type.sort(key=lambda x: x[-1], reverse=True)
                queued_events_by_type_str = ", ".join(f"{m}: {t:,}" for m, t in queued_events_by_type)
                num_queued_events = sum(v for k, v in queued_events_by_type)
                self.scan.info(
                    f"{self.scan.name}: {num_queued_events:,} events in queue ({queued_events_by_type_str})"
                )
            else:
                self.scan.info(f"{self.scan.name}: No events in queue")

            if self.scan.log_level <= logging.DEBUG:
                # status debugging
                scan_active_status = []
                scan_active_status.append(f"scan._finished_init: {self.scan._finished_init}")
                scan_active_status.append(f"manager.active: {self.active}")
                scan_active_status.append(f"    manager.running: {self.running}")
                scan_active_status.append(f"        manager._task_counter.value: {self._task_counter.value}")
                scan_active_status.append(f"        manager._task_counter.tasks:")
                for task in list(self._task_counter.tasks.values()):
                    scan_active_status.append(f"            - {task}:")
                scan_active_status.append(
                    f"        manager.incoming_event_queue.qsize: {self.incoming_event_queue.qsize()}"
                )
                scan_active_status.append(f"    manager.modules_finished: {self.modules_finished}")
                for m in sorted(self.scan.modules.values(), key=lambda m: m.name):
                    running = m.running
                    scan_active_status.append(f"        {m}.finished: {m.finished}")
                    scan_active_status.append(f"            running: {running}")
                    if running:
                        scan_active_status.append(f"            tasks:")
                        for task in list(m._task_counter.tasks.values()):
                            scan_active_status.append(f"                - {task}:")
                    scan_active_status.append(f"            incoming_queue_size: {m.num_incoming_events}")
                    scan_active_status.append(f"            outgoing_queue_size: {m.outgoing_event_queue.qsize()}")
                for line in scan_active_status:
                    self.scan.debug(line)

                # log module memory usage
                module_memory_usage = []
                for module in self.scan.modules.values():
                    memory_usage = module.memory_usage
                    module_memory_usage.append((module.name, memory_usage))
                module_memory_usage.sort(key=lambda x: x[-1], reverse=True)
                self.scan.debug(f"MODULE MEMORY USAGE:")
                for module_name, usage in module_memory_usage:
                    self.scan.debug(f"    - {module_name}: {self.scan.helpers.bytes_to_human(usage)}")

            # Uncomment these lines to enable debugging of event queues

            # queued_events = self.incoming_event_queue.events
            # if queued_events:
            #     queued_events_str = ", ".join(str(e) for e in queued_events)
            #     self.scan.verbose(f"Queued events: {queued_events_str}")
            #     queued_events_by_module = [(k, v) for k, v in self.incoming_event_queue.modules.items() if v > 0]
            #     queued_events_by_module.sort(key=lambda x: x[-1], reverse=True)
            #     queued_events_by_module_str = ", ".join(f"{m}: {t:,}" for m, t in queued_events_by_module)
            #     self.scan.verbose(f"{self.scan.name}: Queued events by module: {queued_events_by_module_str}")

        status.update({"modules_errored": len(modules_errored)})

        return status
