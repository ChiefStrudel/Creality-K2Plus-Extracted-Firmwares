# Code for coordinating events on the printer toolhead
#
# Copyright (C) 2016-2021  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import math, logging, importlib, os, json
import mcu, chelper, kinematics.extruder
from extras.base_info import base_dir
import time
import inspect
import mymodule.mymovie as mymovie
import numpy as np
import ctypes
import cProfile
import pstats
# Common suffixes: _d is distance (in mm), _v is velocity (in
#   mm/second), _v2 is velocity squared (mm^2/s^2), _t is time (in
#   seconds), _r is ratio (scalar between 0.0 and 1.0)

# Class to track each move request

LOOKAHEAD_FLUSH_TIME = 0.250

# Class to track a list of pending move requests and to facilitate
# "look-ahead" across moves to reduce acceleration between moves.
class MoveQueue:
    def __init__(self, toolhead):
        self.toolhead = toolhead
        self.queue = []
        self.junction_flush = LOOKAHEAD_FLUSH_TIME
    def reset(self):
        mymovie.Py_move_queue_del(len(self.queue))
        del self.queue[:]
        self.junction_flush = LOOKAHEAD_FLUSH_TIME
    def set_flush_time(self, flush_time):
        self.junction_flush = flush_time
    def get_last(self):
        if self.queue:
            return self.queue[-1]
        return None
    def flush(self, lazy=False):
        self.junction_flush = LOOKAHEAD_FLUSH_TIME
        # update_flush_count = lazy
        queue = self.queue
        flush_count = len(queue)
        if not flush_count:
            return
        # if flush_count > 150:
        #     profile = cProfile.Profile()
        #     profile.enable()
        starttime = time.time()
        flush_count=mymovie.Py_move_queue_flush_cal(flush_count,lazy)
        # print(f"flush_count:{flush_count} time:{time.time()-starttime}")
        # Generate step times for all moves ready to be flushed
        if flush_count:
            # starttime = time.time()
            self.toolhead._process_moves(queue[:flush_count])
            # print(f"_process_moves flush_count:{flush_count} time:{time.time()-starttime}")
            # Remove processed moves from the queue
            mymovie.Py_move_queue_del(flush_count)
            print(f"c++ flush:{flush_count} time:{time.time()-starttime}")
            del queue[:flush_count]
        # if flush_count > 150:
        #     profile.disable()
        #     stats = pstats.Stats(profile)
        #     stats.sort_stats('cumulative')
        #     stats.print_stats(20)
    def add_move(self, move):
        self.queue.append([move,[]])
        # mymovie.Py_move_queue_add(move)
        if len(self.queue) == 1:
            return
        # pre_move = self.queue[-2][0]
        # if not move.is_kinematic_move or not pre_move.is_kinematic_move:
        #     return
        # extruder_v2 = self.toolhead.extruder.calc_junction(pre_move, move)
        # move.calc_junction(pre_move,extruder_v2)
        self.junction_flush -= move.min_move_t
        if self.junction_flush <= 0.:
            # Enough moves have been queued to reach the target flush time.
            self.flush(lazy=True)

MIN_KIN_TIME = 0.100
MOVE_BATCH_TIME = 0.500
SDS_CHECK_TIME = 0.001 # step+dir+step filter in stepcompress.c

DRIP_SEGMENT_TIME = 0.050
DRIP_TIME = 0.100
class DripModeEndSignal(Exception):
    pass

# Main code to track events (and their timing) on the printer toolhead
class ToolHead:
    def __init__(self, config):
        # double_array_type = ctypes.c_double * 14
        self._double_array = [0.0,0.0,0.0,0.0,
                             0.0,0.0,0.0,0.0,
                             0.0,0.0,0.0,0.0,
                             0.0,0.0]
        # n_items = len(self._double_array)

        # self.double_array = (ctypes.c_double * n_items)(*self._double_array)
        # self.double_array_ptr_int =ctypes.cast(self.double_array, ctypes.c_void_p).value

        self.double_array = np.array(self._double_array, dtype=np.float64)
        self.double_array_ptr_int = self.double_array.ctypes.data
        mymovie.Py_set_cur_move_addr(self.double_array_ptr_int)
        self.config = config
        self.qmode_flag = 0
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.all_mcus = [
            m for n, m in self.printer.lookup_objects(module='mcu')]
        self.mcu = self.all_mcus[0]
        self.can_pause = True
        if self.mcu.is_fileoutput():
            self.can_pause = False
        self.move_queue = MoveQueue(self)
        self.commanded_pos = [0., 0., 0., 0.]
        self.double_array[4]=self.commanded_pos[0]
        self.double_array[5]=self.commanded_pos[1]
        self.double_array[6]=self.commanded_pos[2]
        self.double_array[7]=self.commanded_pos[3]
        self.printer.register_event_handler("klippy:shutdown",
                                            self._handle_shutdown)
        # Velocity and acceleration control
        self.__max_velocity = config.getfloat('max_velocity', above=0.)
        self.double_array[1]=self.__max_velocity
        self.__max_accel = config.getfloat('max_accel', above=0.)
        self.double_array[0]=self.__max_accel
        self.requested_accel_to_decel = config.getfloat(
            'max_accel_to_decel', self.__max_accel * 0.5, above=0.)
        self.__max_accel_to_decel = self.requested_accel_to_decel
        self.double_array[3]=self.__max_accel_to_decel
        self.square_corner_velocity = config.getfloat(
            'square_corner_velocity', 5., minval=0.)
        self.square_corner_max_velocity = config.getfloat(
            'square_corner_max_velocity', 200., minval=0.)
        self.__junction_deviation = 0.
        self.double_array[2]=self.__junction_deviation
        self._calc_junction_deviation()
        # Print time tracking
        self.buffer_time_low = config.getfloat(
            'buffer_time_low', 1.000, above=0.)
        self.buffer_time_high = config.getfloat(
            'buffer_time_high', 2.000, above=self.buffer_time_low)
        self.buffer_time_start = config.getfloat(
            'buffer_time_start', 0.250, above=0.)
        self.move_flush_time = config.getfloat(
            'move_flush_time', 0.050, above=0.)
        self.print_time = 0.
        self.special_queuing_state = "Flushed"
        self.need_check_stall = -1.
        self.flush_timer = self.reactor.register_timer(self._flush_handler)
        self.move_queue.set_flush_time(self.buffer_time_high)
        self.idle_flush_print_time = 0.
        self.print_stall = 0
        self.drip_completion = None
        # Kinematic step generation scan window time tracking
        self.kin_flush_delay = SDS_CHECK_TIME
        self.kin_flush_times = []
        self.last_kin_flush_time = self.last_kin_move_time = 0.
        # Setup iterative solver
        ffi_main, ffi_lib = chelper.get_ffi()
        self.trapq = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
        self.trapq_append = ffi_lib.trapq_append
        self.trapq_finalize_moves = ffi_lib.trapq_finalize_moves
        self.trapq_append_from_moveq = ffi_lib.trapq_append_from_moveq
        self.step_generators = []
        # Create kinematics class
        gcode = self.printer.lookup_object('gcode')
        self.Coord = gcode.Coord
        self.extruder = kinematics.extruder.DummyExtruder(self.printer)
        if hasattr(self.extruder, 'info_array_addr_int'):
            mymovie.Py_set_extruder_info(self.extruder.info_array_addr_int)
        kin_name = config.get('kinematics')
        try:
            mod = importlib.import_module('kinematics.' + kin_name)
            self.kin = mod.load_kinematics(self, config)
        except config.error as e:
            raise
        except self.printer.lookup_object('pins').error as e:
            raise
        except:
            msg = "Error loading kinematics '%s'" % (kin_name,)
            logging.exception(msg)
            raise config.error(msg)
        # Register commands
        gcode.register_command('SET_G29_FLAG', self.cmd_SET_G29_FLAG)
        gcode.register_command('G4', self.cmd_G4)
        gcode.register_command('M400', self.cmd_M400)
        gcode.register_command('SET_VELOCITY_LIMIT',
                               self.cmd_SET_VELOCITY_LIMIT,
                               desc=self.cmd_SET_VELOCITY_LIMIT_help)
        gcode.register_command('M204', self.cmd_M204)
        # Load some default modules
        modules = ["gcode_move", "homing", "idle_timeout", "statistics_ext",
                   "manual_probe", "tuning_tower"]
        for module_name in modules:
            self.printer.load_object(config, module_name)
        self.z_pos_filepath = os.path.join(base_dir, "creality/userdata/config/z_pos.json")
        self.z_pos = self.get_z_pos()
        if self.config.has_section("motor_control") and self.config.getsection('motor_control').getint('switch')==1:
            self.printer.register_event_handler("klippy:ready", self.printer.lookup_object('motor_control').set_motor_pin)
        self.G29_flag = False
    def cmd_SET_G29_FLAG(self, gcmd):
        value = gcmd.get_int('VALUE', 0)
        if value == 1:
            self.G29_flag = True
        else:
            self.G29_flag = False
    def get_max_accel(self):
        return self.__max_accel
    def set_max_accel(self, value):
        self.__max_accel = value
        self.double_array[0]=value
    def get_max_velocity_only(self):
        return self.__max_velocity
    def set_max_velocity(self, value):
        self.__max_velocity = value
        self.double_array[1]=value
    def get_max_accel_to_decel(self):
        return self.__max_accel_to_decel
    def set_max_accel_to_decel(self, value):
        self.__max_accel_to_decel = value
        self.double_array[3]=value
    def get_z_pos(self):
        z_pos = 0
        if os.path.exists(self.z_pos_filepath):
            try:
                with open(self.z_pos_filepath, "r") as f:
                    z_pos = float(json.loads(f.read()).get("z_pos", 0))
            except Exception as err:
                logging.error(err)
        return z_pos
    # Print time tracking
    def _update_move_time(self, next_print_time):
        batch_time = MOVE_BATCH_TIME
        kin_flush_delay = self.kin_flush_delay
        lkft = self.last_kin_flush_time
        while 1:
            self.print_time = min(self.print_time + batch_time, next_print_time)
            sg_flush_time = max(lkft, self.print_time - kin_flush_delay)
            for sg in self.step_generators:
                sg(sg_flush_time)
            free_time = max(lkft, sg_flush_time - kin_flush_delay)
            self.trapq_finalize_moves(self.trapq, free_time)
            self.extruder.update_move_time(free_time)
            mcu_flush_time = max(lkft, sg_flush_time - self.move_flush_time)
            for m in self.all_mcus:
                m.flush_moves(mcu_flush_time)
            if self.print_time >= next_print_time:
                break
    def _calc_print_time(self):
        curtime = self.reactor.monotonic()
        est_print_time = self.mcu.estimated_print_time(curtime)
        kin_time = max(est_print_time + MIN_KIN_TIME, self.last_kin_flush_time)
        kin_time += self.kin_flush_delay
        min_print_time = max(est_print_time + self.buffer_time_start, kin_time)
        if min_print_time > self.print_time:
            self.print_time = min_print_time
            self.printer.send_event("toolhead:sync_print_time",
                                    curtime, est_print_time, self.print_time)
    def _process_moves(self, moves):
        # Resync print_time if necessary
        if self.special_queuing_state:
            if self.special_queuing_state != "Drip":
                # Transition from "Flushed"/"Priming" state to main state
                self.special_queuing_state = ""
                self.need_check_stall = -1.
                self.reactor.update_timer(self.flush_timer, self.reactor.NOW)
            self._calc_print_time()
        # Queue moves into trapezoid motion queue (trapq)
        next_move_time = self.print_time
        # start=time.time()
        return_value=self.trapq_append_from_moveq(self.trapq,self.extruder.trapq,next_move_time,mymovie.Py_get_moveq_only_data_buffer(),len(moves))
        if return_value.extru_last_position < 109999999:
            self.extruder.last_position=return_value.extru_last_position
        # for move in moves:
        #     _move=move[0]
        #     _move_cb=move[1]
        #     if _move.is_kinematic_move:
        #         self.trapq_append(
        #             self.trapq, next_move_time,
        #             _move.accel_t, _move.cruise_t, _move.decel_t,
        #             _move.start_pos[0], _move.start_pos[1], _move.start_pos[2],
        #             _move.axes_r[0], _move.axes_r[1], _move.axes_r[2],
        #             _move.start_v, _move.cruise_v, _move.accel)
        #     if _move.axes_d[3]:
        #         self.extruder.move(next_move_time, _move)
        #     next_move_time = (next_move_time + _move.accel_t
        #                       + _move.cruise_t + _move.decel_t)
            # for cb in _move_cb:
            #     cb(next_move_time)
        # time.sleep(0.100)
        # print(f"trapq_append:{time.time()-start}")
        # Generate steps for moves
        if self.special_queuing_state:
            self._update_drip_move_time(return_value.next_move_time)
        self._update_move_time(return_value.next_move_time)
        self.last_kin_move_time = return_value.next_move_time
    def flush_step_generation(self):
        # Transition from "Flushed"/"Priming"/main state to "Flushed" state
        self.move_queue.flush()
        self.special_queuing_state = "Flushed"
        self.need_check_stall = -1.
        self.reactor.update_timer(self.flush_timer, self.reactor.NEVER)
        self.move_queue.set_flush_time(self.buffer_time_high)
        self.idle_flush_print_time = 0.
        flush_time = self.last_kin_move_time + self.kin_flush_delay
        flush_time = max(flush_time, self.print_time - self.kin_flush_delay)
        self.last_kin_flush_time = max(self.last_kin_flush_time, flush_time)
        self._update_move_time(max(self.print_time, self.last_kin_flush_time))
    def _flush_lookahead(self):
        if self.special_queuing_state:
            return self.flush_step_generation()
        self.move_queue.flush()
    def get_last_move_time(self):
        self._flush_lookahead()
        if self.special_queuing_state:
            self._calc_print_time()
        return self.print_time
    def _check_stall(self):
        eventtime = self.reactor.monotonic()
        if self.special_queuing_state:
            if self.idle_flush_print_time:
                # Was in "Flushed" state and got there from idle input
                est_print_time = self.mcu.estimated_print_time(eventtime)
                if est_print_time < self.idle_flush_print_time:
                    self.print_stall += 1
                self.idle_flush_print_time = 0.
            # Transition from "Flushed"/"Priming" state to "Priming" state
            self.special_queuing_state = "Priming"
            self.need_check_stall = -1.
            self.reactor.update_timer(self.flush_timer, eventtime + 0.100)
        # Check if there are lots of queued moves and stall if so
        while 1:
            est_print_time = self.mcu.estimated_print_time(eventtime)
            buffer_time = self.print_time - est_print_time
            stall_time = buffer_time - self.buffer_time_high
            if stall_time <= 0.:
                break
            if not self.can_pause:
                self.need_check_stall = self.reactor.NEVER
                return
            eventtime = self.reactor.pause(eventtime + min(1., stall_time))
        if not self.special_queuing_state:
            # In main state - defer stall checking until needed
            self.need_check_stall = (est_print_time + self.buffer_time_high
                                     + 0.100)
    def _flush_handler(self, eventtime):
        try:
            print_time = self.print_time
            buffer_time = print_time - self.mcu.estimated_print_time(eventtime)
            if buffer_time > self.buffer_time_low:
                # Running normally - reschedule check
                return eventtime + buffer_time - self.buffer_time_low
            # Under ran low buffer mark - flush lookahead queue
            self.flush_step_generation()
            if print_time != self.print_time:
                self.idle_flush_print_time = self.print_time
        except:
            logging.exception("Exception in flush_handler")
            self.printer.invoke_shutdown("Exception in flush_handler")
        return self.reactor.NEVER
    # Movement commands
    def get_position(self):
        return list(self.commanded_pos)
    def set_position(self, newpos, homing_axes=()):
        self.flush_step_generation()
        ffi_main, ffi_lib = chelper.get_ffi()
        ffi_lib.trapq_set_position(self.trapq, self.print_time,
                                   newpos[0], newpos[1], newpos[2])
        self.commanded_pos[:] = newpos
        self.double_array[4]=self.commanded_pos[0]
        self.double_array[5]=self.commanded_pos[1]
        self.double_array[6]=self.commanded_pos[2]
        self.double_array[7]=self.commanded_pos[3]
        self.kin.set_position(newpos, homing_axes)
        self.printer.send_event("toolhead:set_position")
    def record_z_pos(self, commanded_pos_z):
        if(self.kin.__class__.__name__ == "CoreXYKinematics"):
            if(self.kin.get_status_for_record_z_pos()):
                if abs(commanded_pos_z-self.z_pos) > 5:
                    self.z_pos = commanded_pos_z
                    with open(self.z_pos_filepath, "w") as f:
                        f.write(json.dumps({"z_pos": commanded_pos_z}))
                        f.flush()
                    print_stats = self.printer.lookup_object('print_stats', None)
                    print_stats.z_pos = self.z_pos
                    logging.info("record_z_pos:%s" % commanded_pos_z)
            return
        else:
            curtime = self.printer.get_reactor().monotonic()
            kin_status = self.kin.get_status(curtime)
        if ('z' in kin_status['homed_axes']):
            try:
                if abs(commanded_pos_z-self.z_pos) > 5:
                    self.z_pos = commanded_pos_z
                    with open(self.z_pos_filepath, "w") as f:
                        f.write(json.dumps({"z_pos": commanded_pos_z}))
                        f.flush()
                    print_stats = self.printer.lookup_object('print_stats', None)
                    print_stats.z_pos = self.z_pos
                    logging.info("record_z_pos:%s" % commanded_pos_z)
            except Exception as err:
                logging.error(err)
    def check_move_out_of_range(self, ep):
        toolhead = self.printer.lookup_object('toolhead')
        code_key = "key243"
        min_x = toolhead.kin.limits[0][0]
        max_x = toolhead.kin.limits[0][1]
        min_y = toolhead.kin.limits[1][0]
        max_y = toolhead.kin.limits[1][1]
        min_z = toolhead.kin.limits[2][0]
        max_z = toolhead.kin.limits[2][1]
        if min_x > ep[0] or ep[0] > max_x:
            code_key = "key585"
        elif min_y > ep[1] or ep[1] > max_y:
            code_key = "key586"
        elif min_z > ep[2] or ep[2] > max_z:
            code_key = "key587"
        msg="Move out of range"
        logging.info("stepper xyz min_x:%s max_x:%s|min_y:%s max_y:%s|min_z:%s max_z:%s, toolhead.kin.limits:%s" % (min_x, max_x, min_y, max_y, min_z, max_z, str(toolhead.kin.limits)))
        m = """{"code":"%s","msg":"%s: %.3f %.3f %.3f [%.3f]", "values":[%.3f, %.3f, %.3f, %.3f]}""" % (
            code_key, msg, ep[0], ep[1], ep[2], ep[3], ep[0], ep[1], ep[2], ep[3])
        return m
    def simple_move(self, newpos):
        # print("get #################################################move: %s %s" % (newpos, speed))
        self.record_z_pos(newpos[2])
        # starttime=time.time()
        
        
        # self.double_array[12]=speed
        # print(f"time cost of move: {time.time()-starttime}")
        
        # starttime=time.time()
        move = mymovie.PyMove()
        # print(f"time cost of PyMove: {time.time()-starttime}")
        
        if self.double_array[13]==0:
            self.commanded_pos[:] = newpos
        elif self.double_array[13]==1:
            self.commanded_pos[3] = newpos[3]
        elif self.double_array[13]==-4:
            raise self.printer.command_error("""{"code":"key111", "msg": "Extrude below minimum temp\nSee the 'min_extrude_temp' config option for details", "values": []}""")
        elif self.double_array[13]==-2:
            raise self.printer.command_error("Must home axis first")
        elif self.double_array[13]==-3:
            m = self.check_move_out_of_range(newpos)
            raise self.printer.command_error(m)
            # raise self.printer.command_error("Move out of range")
        elif self.double_array[13]==-1:
            return
        elif self.double_array[13]==-5:
            raise self.printer.command_error("Extrude only move too long")
        elif self.double_array[13]==-6:
            raise self.printer.command_error("""{"code":"key112", "msg": "Move exceeds maximum extrusion (%.3fmm^2 vs %.3fmm^2)\nSee the 'max_extrude_cross_section' config option for details", "values": [%.3f, %.3f]}""")
        # if not move.move_d:
        #     return
        # if move.is_kinematic_move:
        #     self.kin.check_move(move)
        # if move.axes_d[3]:
        #     self.extruder.check_move(move)
        # self.commanded_pos[:] = move.end_pos
        self.move_queue.add_move(move)
        if self.print_time > self.need_check_stall:
            # print(f"before _check_stall: {self.print_time} {self.need_check_stall} {self.special_queuing_state}")
            self._check_stall()
            # print(f"after _check_stall: {self.print_time} {self.need_check_stall} {self.special_queuing_state}")
    def move(self, newpos, speed):
        # print("get #################################################move: %s %s" % (newpos, speed))
        self.record_z_pos(newpos[2])
        # starttime=time.time()
        # print(f"move:{self.commanded_pos} {newpos} {speed}")
        self.double_array[4]=self.commanded_pos[0]
        self.double_array[5]=self.commanded_pos[1]
        self.double_array[6]=self.commanded_pos[2]
        self.double_array[7]=self.commanded_pos[3]
        self.double_array[8]=newpos[0]
        self.double_array[9]=newpos[1]
        self.double_array[10]=newpos[2]
        self.double_array[11]=newpos[3]
        self.double_array[12]=speed
        # print(f"time cost of move: {time.time()-starttime}")
        
        # starttime=time.time()
        move = mymovie.PyMove()
        # print(f"time cost of PyMove: {time.time()-starttime}")
        # print(f"return:{self.double_array[13]}")
        if self.double_array[13]==0:
            self.commanded_pos[:] = newpos
        elif self.double_array[13]==1:
            self.commanded_pos[3] = newpos[3]
        elif self.double_array[13]==-4:
            raise self.printer.command_error("""{"code":"key111", "msg": "Extrude below minimum temp\nSee the 'min_extrude_temp' config option for details", "values": []}""")
        elif self.double_array[13]==-2:
            raise self.printer.command_error("Must home axis first")
        elif self.double_array[13]==-3:
            m = self.check_move_out_of_range(newpos)
            raise self.printer.command_error(m)
            # raise self.printer.command_error("Move out of range")
        elif self.double_array[13]==-1:
            return
        elif self.double_array[13]==-5:
            raise self.printer.command_error("Extrude only move too long")
        elif self.double_array[13]==-6:
            raise self.printer.command_error("""{"code":"key112", "msg": "Move exceeds maximum extrusion (%.3fmm^2 vs %.3fmm^2)\nSee the 'max_extrude_cross_section' config option for details", "values": [%.3f, %.3f]}""")
        # if not move.move_d:
        #     return
        # if move.is_kinematic_move:
        #     self.kin.check_move(move)
        # if move.axes_d[3]:
        #     self.extruder.check_move(move)
        # self.commanded_pos[:] = move.end_pos
        self.move_queue.add_move(move)
        if self.print_time > self.need_check_stall:
            self._check_stall()
    def manual_move(self, coord, speed):
        curpos = list(self.commanded_pos)
        for i in range(len(coord)):
            if coord[i] is not None:
                curpos[i] = coord[i]
        self.move(curpos, speed)
        self.printer.send_event("toolhead:manual_move")
    def dwell(self, delay):
        next_print_time = self.get_last_move_time() + max(0., delay)
        self._update_move_time(next_print_time)
        self._check_stall()
    def wait_moves(self):
        self._flush_lookahead()
        eventtime = self.reactor.monotonic()
        while (not self.special_queuing_state
               or self.print_time >= self.mcu.estimated_print_time(eventtime)):
            if not self.can_pause:
                break
            eventtime = self.reactor.pause(eventtime + 0.100)
    def set_extruder(self, extruder, extrude_pos):
        self.extruder = extruder
        if hasattr(self.extruder, 'info_array_addr_int'):
            mymovie.Py_set_extruder_info(self.extruder.info_array_addr_int)
        self.commanded_pos[3] = extrude_pos
        self.double_array[7]=self.commanded_pos[3]
    def get_extruder(self):
        return self.extruder
    # Homing "drip move" handling
    def _update_drip_move_time(self, next_print_time):
        flush_delay = DRIP_TIME + self.move_flush_time + self.kin_flush_delay
        while self.print_time < next_print_time:
            if self.drip_completion.test():
                raise DripModeEndSignal()
            curtime = self.reactor.monotonic()
            est_print_time = self.mcu.estimated_print_time(curtime)
            wait_time = self.print_time - est_print_time - flush_delay
            if wait_time > 0. and self.can_pause:
                # Pause before sending more steps
                self.drip_completion.wait(curtime + wait_time)
                continue
            npt = min(self.print_time + DRIP_SEGMENT_TIME, next_print_time)
            self._update_move_time(npt)
    def drip_move(self, newpos, speed, drip_completion):
        self.dwell(self.kin_flush_delay)
        # Transition from "Flushed"/"Priming"/main state to "Drip" state
        self.move_queue.flush()
        self.special_queuing_state = "Drip"
        self.need_check_stall = self.reactor.NEVER
        self.reactor.update_timer(self.flush_timer, self.reactor.NEVER)
        self.move_queue.set_flush_time(self.buffer_time_high)
        self.idle_flush_print_time = 0.
        self.drip_completion = drip_completion
        # Submit move
        try:
            self.move(newpos, speed)
        except self.printer.command_error as e:
            self.flush_step_generation()
            raise
        # Transmit move in "drip" mode
        try:
            self.move_queue.flush()
        except DripModeEndSignal as e:
            self.move_queue.reset()
            self.trapq_finalize_moves(self.trapq, self.reactor.NEVER)
        # Exit "Drip" state
        self.flush_step_generation()
    # Misc commands
    def stats(self, eventtime):
        for m in self.all_mcus:
            m.check_active(self.print_time, eventtime)
        buffer_time = self.print_time - self.mcu.estimated_print_time(eventtime)
        is_active = buffer_time > -60. or not self.special_queuing_state
        if self.special_queuing_state == "Drip":
            buffer_time = 0.
        return is_active, "print_time=%.3f buffer_time=%.3f print_stall=%d" % (
            self.print_time, max(buffer_time, 0.), self.print_stall)
    def check_busy(self, eventtime):
        est_print_time = self.mcu.estimated_print_time(eventtime)
        lookahead_empty = not self.move_queue.queue
        return self.print_time, est_print_time, lookahead_empty
    def get_status(self, eventtime):
        print_time = self.print_time
        estimated_print_time = self.mcu.estimated_print_time(eventtime)
        res = dict(self.kin.get_status(eventtime))
        res.update({ 'print_time': print_time,
                     'stalls': self.print_stall,
                     'estimated_print_time': estimated_print_time,
                     'extruder': self.extruder.get_name(),
                     'position': self.Coord(*self.commanded_pos),
                     'max_velocity': self.__max_velocity,
                     'max_accel': self.__max_accel,
                     'max_accel_to_decel': self.requested_accel_to_decel,
                     'square_corner_velocity': self.square_corner_velocity,
                     "G29_flag": self.G29_flag})
        return res
    def _handle_shutdown(self):
        self.can_pause = False
        self.move_queue.reset()
    def get_kinematics(self):
        return self.kin
    def get_trapq(self):
        return self.trapq
    def register_step_generator(self, handler):
        self.step_generators.append(handler)
    def note_step_generation_scan_time(self, delay, old_delay=0.):
        self.flush_step_generation()
        cur_delay = self.kin_flush_delay
        if old_delay:
            self.kin_flush_times.pop(self.kin_flush_times.index(old_delay))
        if delay:
            self.kin_flush_times.append(delay)
        new_delay = max(self.kin_flush_times + [SDS_CHECK_TIME])
        self.kin_flush_delay = new_delay
    def register_lookahead_callback(self, callback):
        last_move = self.move_queue.get_last()
        if last_move is None:
            callback(self.get_last_move_time())
            return
        last_move[1].append(callback)
    def note_kinematic_activity(self, kin_time):
        self.last_kin_move_time = max(self.last_kin_move_time, kin_time)
    def get_max_velocity(self):
        return self.__max_velocity, self.__max_accel
    def _calc_junction_deviation(self):
        scv2 = self.square_corner_velocity**2
        self.__junction_deviation = scv2 * (math.sqrt(2.) - 1.) / self.__max_accel
        self.double_array[2]=self.__junction_deviation
        self.__max_accel_to_decel = min(self.requested_accel_to_decel,
                                      self.__max_accel)
        self.double_array[3]=self.__max_accel_to_decel
    def cmd_G4(self, gcmd):
        # Dwell
        delay = gcmd.get_float('P', 0., minval=0.) / 1000.
        self.dwell(delay)
    def cmd_M400(self, gcmd):
        # Wait for current moves to finish
        self.wait_moves()
    cmd_SET_VELOCITY_LIMIT_help = "Set printer velocity limits"
    def cmd_SET_VELOCITY_LIMIT(self, gcmd):

        qmode_max_accel = 0
        qmode_max_accel_to_decel = 0

        custom_macro = self.printer.lookup_object('custom_macro')
        self.qmode_flag = custom_macro.qmode_flag

        if self.config.has_section('gcode_macro Qmode'):
            Qmode = self.config.getsection('gcode_macro Qmode')
            qmode_max_accel = Qmode.getfloat('variable_max_accel')
            qmode_max_accel_to_decel = Qmode.getfloat('variable_max_accel_to_decel')
            # gcmd.respond_info("SET_VELOCITY_LIMIT] qmode_flag={}".format(self.qmode_flag))
            # gcmd.respond_info("SET_VELOCITY_LIMIT] qmode_max_accel={}".format(qmode_max_accel))
            # gcmd.respond_info("SET_VELOCITY_LIMIT] qmode_max_accel_to_decel={}".format(qmode_max_accel_to_decel))

        max_velocity = gcmd.get_float('VELOCITY', None, above=0.)
        max_accel = gcmd.get_float('ACCEL', None, above=0.)
        square_corner_velocity = gcmd.get_float(
            'SQUARE_CORNER_VELOCITY', None, minval=0.)
        requested_accel_to_decel = gcmd.get_float(
            'ACCEL_TO_DECEL', None, above=0.)
        if max_velocity is not None:
            self.__max_velocity = max_velocity
            self.double_array[1]=self.__max_velocity
        if max_accel is not None:
            if self.qmode_flag and max_accel > qmode_max_accel:
                self.__max_accel = qmode_max_accel
            else:
                self.__max_accel = max_accel
            self.double_array[0]=self.__max_accel
            # gcmd.respond_info("SET_VELOCITY_LIMIT] self.__max_accel={}".format(self.__max_accel))
        if square_corner_velocity is not None:
            if square_corner_velocity > self.square_corner_max_velocity:
                square_corner_velocity = self.square_corner_max_velocity
            self.square_corner_velocity = square_corner_velocity
        if requested_accel_to_decel is not None:
            if self.qmode_flag and requested_accel_to_decel > qmode_max_accel_to_decel:
                self.requested_accel_to_decel = qmode_max_accel_to_decel
            else:
                self.requested_accel_to_decel = requested_accel_to_decel
            # gcmd.respond_info("SET_VELOCITY_LIMIT] self.requested_accel_to_decel={}".format(self.requested_accel_to_decel))

        self._calc_junction_deviation()
        # msg = ("max_velocity: %.6f\n"
        #        "max_accel: %.6f\n"
        #        "max_accel_to_decel: %.6f\n"
        #        "square_corner_velocity: %.6f" % (
        #            self.max_velocity, self.max_accel,
        #            self.requested_accel_to_decel,
        #            self.square_corner_velocity))
        # self.printer.set_rollover_info("toolhead", "toolhead: %s" % (msg,))
        if (max_velocity is None and
            max_accel is None and
            square_corner_velocity is None and
            requested_accel_to_decel is None):
            gcmd.respond_info(msg, log=False)
    def cmd_M204(self, gcmd):
        accel_S = int(float(gcmd.get('S', -1)))
        if accel_S != -1 and accel_S <= 100:
            accel = 100
        else:
            accel = gcmd.get_float('S', None, above=0.)
        # Use S for accel
        # accel = gcmd.get_float('S', None, above=0.)
        cmd = "M204 S%s" % accel
        if accel is None:
            # Use minimum of P and T for accel
            p = gcmd.get_float('P', None, above=0.)
            t = gcmd.get_float('T', None, above=0.)
            if p is None or t is None:
                gcmd.respond_info("""{"code":"key73", "msg": "Invalid M204 command "%s"", "values": ["%s"]}"""
                                  % (gcmd.get_commandline(),gcmd.get_commandline()))
                return
            accel = min(p, t)
            cmd = "M204 P%s T%s" % (p, t)
        self.__max_accel = accel
        self.double_array[0]=self.__max_accel
        self._calc_junction_deviation()
        v_sd = self.printer.lookup_object('virtual_sdcard', None)
        print_stats = self.printer.lookup_object('print_stats', None)
        if print_stats and print_stats.state == "printing" and v_sd and v_sd.count_M204 < 3 and os.path.exists(v_sd.print_file_name_path):
            v_sd.count_M204 += 1
            with open(v_sd.print_file_name_path, "r") as f:
                result = (json.loads(f.read()))
                result["M204"] = cmd
            with open(v_sd.print_file_name_path, "w") as f:
                f.write(json.dumps(result))
                f.flush()
            logging.info("Record cmd_M204")

def add_printer_objects(config):
    config.get_printer().add_object('toolhead', ToolHead(config))
    kinematics.extruder.add_printer_objects(config)