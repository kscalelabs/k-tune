#!/usr/bin/env python3
"""
ktune - A CLI tool for running simple actuator tests (sine or step).
Example usage:
    # Sine test:
    ./ktune.py --actuator_id 11 --test sine --freq 1.0 --amp 5.0 --duration 5.0

    # Step test:
    ./ktune.py --actuator_id 11 --test step --step_size 10.0 --step_hold_time 1.0 --step_count 2

    # See --help for all options.
"""

import argparse
import asyncio
from multiprocessing import Process, Queue
import math
import time
import json
import os
import numpy as np
from datetime import datetime
import matplotlib.pyplot as plt

from pykos import KOS


#######################
# DATA COLLECTION     #
#######################
async def collect_data(kos: KOS, actuator_id: int, data_dict: dict,
                       stop_time: float, sample_rate=100.0,
                       test_type="sine", freq=1.0, amp=5.0, offset=0.0,
                       start_time: float = None):
    dt = 1.0 / sample_rate
    # Use provided start_time (global) so that all data uses the same time reference.
    if start_time is None:
        start_time = time.time()
    next_sample = start_time

    while True:
        now = time.time()
        if now > stop_time:
            break

        response = await kos.actuator.get_actuators_state([actuator_id])
        state_list = response.states
        if state_list:
            state = state_list[0]
            elapsed = now - start_time
            data_dict["time"].append(elapsed)
            data_dict["position"].append(state.position if state.position is not None else float('nan'))
            data_dict["velocity"].append(state.velocity if state.velocity is not None else float('nan'))
        next_sample += dt
        sleep_time = next_sample - time.time()
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)


#############################
# TUNING METRICS  #
#############################
def compute_step_overshoots(time_array, pos_array, steps, window_duration=1.0):
    """
    For each step transition, compute the overshoot percentage based on the maximum (or minimum)
    position reached within a fixed window (default: 1 second) after the new target is commanded.

    For an upward step (new_target > old_target):
      overshoot (%) = (peak - new_target) / (new_target - old_target) * 100
    For a downward step (new_target < old_target):
      overshoot (%) = (new_target - trough) / (old_target - new_target) * 100

    :param time_array: Array of time stamps (seconds)
    :param pos_array:  Array of measured positions (degrees)
    :param steps:      List of tuples (target, duration) that define the step sequence.
                       The first element defines the starting position.
    :param window_duration: Duration (in seconds) after the new command to look for the peak.
    :return: List of overshoot percentages (one per transition).
    """
    import numpy as np

    time_array = np.array(time_array)
    pos_array  = np.array(pos_array)

    # Build an array of step command times (cumulative durations)
    step_times = [0.0]
    for (target, velocity, duration) in steps:
        step_times.append(step_times[-1] + duration)

    overshoots = []
    # For each step transition (i.e. from step i-1 to i)
    for i in range(1, len(steps)):
        old_target = steps[i-1][0]
        new_target = steps[i][0]
        command_time = step_times[i]
        window_end = command_time + window_duration

        # Find indices of data within [command_time, command_time + window_duration]
        idx = np.where((time_array >= command_time) & (time_array <= window_end))[0]
        if len(idx) == 0:
            continue
        p_window = pos_array[idx]

        if new_target > old_target:
            # For an upward step, overshoot is defined by the maximum value in the window.
            peak = np.max(p_window)
            overshoot = (peak - new_target) / (new_target - old_target) * 100.0
        else:
            # For a downward step, overshoot is defined by the minimum value in the window.
            trough = np.min(p_window)
            overshoot = (new_target - trough) / (old_target - new_target) * 100.0

        # Clamp negative overshoot to zero.
        overshoots.append(max(0.0, overshoot))

    return overshoots

#############################
# ACTUATOR TEST FUNCTIONS   #
#############################
async def run_sine_test(kos: KOS,
                        actuator_id: int,
                        amplitude: float,
                        freq: float,
                        duration: float,
                        kp: float,
                        kd: float,
                        ki: float,
                        sim_kp: float,
                        sim_kv: float,
                        acceleration: float,
                        max_torque: float,
                        torque_enabled: bool,
                        update_rate: float,
                        data_dict: dict,
                        start_time: float,
                        is_real: bool,
                        request_state: bool = True):
    """
    Command a sine wave and log timestamps, commanded, and measured values.
    Uses simulation gains (sim_kp, sim_kv) if is_real is False.
    When request_state is False, measured values are set equal to commanded values.
    """
    # Choose which gains to use
    if is_real:
        used_kp = kp
        used_kd = kd
        used_ki = ki
    else:
        used_kp = sim_kp
        used_kd = sim_kv
        used_ki = 0

    # Configure actuator
    if is_real:
        await kos.actuator.configure_actuator(
            actuator_id=actuator_id,
            kp=used_kp, kd=used_kd, ki=used_ki,
            acceleration=acceleration,
            max_torque=max_torque,
            torque_enabled=torque_enabled
        )
    else:
        await kos.actuator.configure_actuator(
            actuator_id=actuator_id,
            kp=used_kp, kd=used_kd,
            max_torque=max_torque,
            torque_enabled=torque_enabled
        )

    print(f"Real: {is_real}, Frequency: {freq}, Amplitude: {amplitude}, Duration: {duration}")

    dt = 1.0 / update_rate
    steps = int(duration / dt)
    
    # Ensure keys exist in data_dict.
    data_dict.setdefault("cmd_time", [])
    data_dict.setdefault("resp_time", [])
    data_dict.setdefault("cmd_pos", [])
    data_dict.setdefault("cmd_vel", [])
    data_dict.setdefault("time", [])
    data_dict.setdefault("position", [])
    data_dict.setdefault("velocity", [])
    
    next_tick = time.time()
    
    for i in range(steps):
        t = i * dt
        angle = amplitude * math.sin(2.0 * math.pi * freq * t)
        vel = amplitude * (2.0 * math.pi * freq) * math.cos(2.0 * math.pi * freq * t)

        # Log command time
        t_send = time.time() - start_time
        data_dict["cmd_time"].append(t_send)
        data_dict["cmd_pos"].append(angle)
        data_dict["cmd_vel"].append(vel)
        
        # Send the command
        await kos.actuator.command_actuators([
            {'actuator_id': actuator_id, 'position': angle, 'velocity': vel}
        ])

        # Immediately query state if enabled; otherwise use commanded values.
        if request_state:
            response = await kos.actuator.get_actuators_state([actuator_id])
            t_resp = time.time() - start_time
            data_dict["resp_time"].append(t_resp)
            if response.states:
                state = response.states[0]
                measured_pos = state.position
                measured_vel = state.velocity
            else:
                measured_pos, measured_vel = float('nan'), float('nan')
        else:
            t_resp = time.time() - start_time
            data_dict["resp_time"].append(t_resp)
            measured_pos, measured_vel = angle, vel
        
        # Log measured state
        data_dict["time"].append(t_resp)
        data_dict["position"].append(measured_pos)
        data_dict["velocity"].append(measured_vel)
        
        
        next_tick += dt
        sleep_time = next_tick - time.time()
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)




# Updated run_step_test signature and configuration:
async def run_step_test(
    kos: KOS,
    actuator_id: int,
    step_size: float,
    step_hold_time: float,
    step_count: int,
    kp: float,
    kd: float,
    ki: float,
    sim_kp: float,
    sim_kv: float,
    acceleration: float,
    max_torque: float,
    torque_enabled: bool = True,
    vel_limit: float = 200.0,
    data_dict: dict = None,
    start_time: float = None,
    sample_rate: float = 50.0,
    is_real: bool = True
):
    """
    Perform a step test with continuous sampling during hold periods.
    Uses simulation gains if is_real is False.
    """
    if is_real:
        used_kp = kp
        used_kd = kd
        used_ki = ki
    else:
        used_kp = sim_kp
        used_kd = sim_kv
        used_ki = ki


    if start_time is None:
        start_time = time.time()
    
    # Ensure keys exist in data_dict.
    data_dict.setdefault("cmd_time", [])
    data_dict.setdefault("cmd_pos", [])
    data_dict.setdefault("cmd_vel", [])
    data_dict.setdefault("time", [])
    data_dict.setdefault("position", [])
    data_dict.setdefault("velocity", [])
    
    # Configure the actuator
    await kos.actuator.configure_actuator(
        actuator_id=actuator_id,
        kp=used_kp, kd=used_kd, ki=used_ki,
        acceleration=acceleration,
        max_torque=max_torque,
        torque_enabled=torque_enabled
    )
    sample_period = 1.0 / sample_rate

    # Initial hold at 0°.
    t_send = time.time() - start_time
    data_dict["cmd_time"].append(t_send)
    data_dict["cmd_pos"].append(0.0)
    data_dict["cmd_vel"].append(vel_limit)
    data_dict["time"].append(t_send)
    data_dict["position"].append(0.0)
    data_dict["velocity"].append(0.0)


    await kos.actuator.command_actuators([
        {'actuator_id': actuator_id, 'position': 0.0}
    ])
    

    # Continuously sample during the initial hold.
    end_hold = time.time() + step_hold_time
    while time.time() < end_hold:
        response = await kos.actuator.get_actuators_state([actuator_id])
        t_resp = time.time() - start_time
        if response.states:
            state = response.states[0]
            measured_pos = state.position if state.position is not None else float('nan')
            measured_vel = state.velocity if state.velocity is not None else float('nan')
            if not is_real:
                print (measured_pos, measured_vel)
        else:
            measured_pos, measured_vel = float('nan'), float('nan')
        data_dict["time"].append(t_resp)
        data_dict["position"].append(measured_pos)
        data_dict["velocity"].append(measured_vel)
        data_dict["cmd_time"].append(t_resp)
        data_dict["cmd_pos"].append(0.0)
        data_dict["cmd_vel"].append(vel_limit)
        await asyncio.sleep(sample_period)
    
    # Now do repeated step cycles.
    for _ in range(step_count):
        # STEP UP
        t_send = time.time() - start_time
        data_dict["cmd_time"].append(t_send)
        data_dict["cmd_pos"].append(step_size)
        data_dict["cmd_vel"].append(vel_limit)
        data_dict["time"].append(t_send)
        data_dict["position"].append(0.0)
        data_dict["velocity"].append(0.0)

        await kos.actuator.command_actuators([
            {'actuator_id': actuator_id, 'position': step_size}
        ])
    
        # Sample continuously during the hold period.
        end_hold = time.time() + step_hold_time
        while time.time() < end_hold:
            response = await kos.actuator.get_actuators_state([actuator_id])
            t_resp = time.time() - start_time
            if response.states:
                state = response.states[0]
                measured_pos = state.position if state.position is not None else float('nan')
                measured_vel = state.velocity if state.velocity is not None else float('nan')
            else:
                measured_pos, measured_vel = float('nan'), float('nan')
            data_dict["time"].append(t_resp)
            data_dict["position"].append(measured_pos)
            data_dict["velocity"].append(measured_vel)
            data_dict["cmd_time"].append(t_resp)
            data_dict["cmd_pos"].append(step_size)
            data_dict["cmd_vel"].append(vel_limit)
            await asyncio.sleep(sample_period)
    
        # STEP DOWN
        t_send = time.time() - start_time
        data_dict["cmd_time"].append(t_send)
        data_dict["cmd_pos"].append(0.0)
        data_dict["cmd_vel"].append(vel_limit)
        data_dict["time"].append(t_send)
        data_dict["position"].append(step_size)
        data_dict["velocity"].append(0.0)
        await kos.actuator.command_actuators([
            {'actuator_id': actuator_id, 'position': 0.0}
        ])
 
        # Sample continuously during the hold period.
        end_hold = time.time() + step_hold_time
        while time.time() < end_hold:
            response = await kos.actuator.get_actuators_state([actuator_id])
            t_resp = time.time() - start_time
            if response.states:
                state = response.states[0]
                measured_pos = state.position if state.position is not None else float('nan')
                measured_vel = state.velocity if state.velocity is not None else float('nan')
            else:
                measured_pos, measured_vel = float('nan'), float('nan')
            data_dict["time"].append(t_resp)
            data_dict["position"].append(measured_pos)
            data_dict["velocity"].append(measured_vel)
            data_dict["cmd_time"].append(t_resp)
            data_dict["cmd_pos"].append(0.0)
            data_dict["cmd_vel"].append(vel_limit)
            await asyncio.sleep(sample_period)

#############################
# SIMULATOR TEST #
#############################
def run_sim_test(args, global_start, out_queue):
    sim_data = {"time": [], "position": [], "velocity": [], "cmd_time": [], "cmd_pos": [], "cmd_vel": []}
    # Run the simulator test; here we'll assume a sine test
    if args.test == "sine":
        asyncio.run(run_sine_test(
            kos=KOS(args.sim_ip),
            actuator_id=args.actuator_id,
            amplitude=args.amp,
            freq=args.freq,
            duration=args.duration,
            kp=args.kp,
            kd=args.kd,
            ki=args.ki,
            sim_kp=args.sim_kp,
            sim_kv=args.sim_kv,
            acceleration=args.acceleration,
            max_torque=args.max_torque,
            torque_enabled=(not args.torque_off),
            update_rate=50.0,
            data_dict=sim_data,
            start_time=global_start,
            is_real=False,
            request_state=True
        ))
    elif args.test == "step":
        asyncio.run(run_step_test(
            kos=KOS(args.sim_ip),
            actuator_id=args.actuator_id,
            step_size=args.step_size,
            step_hold_time=args.step_hold_time,
            step_count=args.step_count,
            kp=args.kp,
            kd=args.kd,
            ki=args.ki,
            sim_kp=args.sim_kp,
            sim_kv=args.sim_kv,
            acceleration=args.acceleration,
            max_torque=args.max_torque,
            torque_enabled=(not args.torque_off),
            vel_limit=400.0,
            data_dict=sim_data,
            start_time=global_start,
            sample_rate=args.sample_rate,
            is_real=False
        ))
    out_queue.put(sim_data)

#############################
# REAL ROBOT TEST #
#############################
def run_real_test(args, global_start, out_queue):
    real_data = {"time": [], "position": [], "velocity": [], "cmd_time": [], "cmd_pos": [], "cmd_vel": []}
    # Run the real robot test; here we'll assume a sine test
    if args.test == "sine":
        asyncio.run(run_sine_test(
            kos=KOS(args.ip),
            actuator_id=args.actuator_id,
            amplitude=args.amp,
            freq=args.freq,
            duration=args.duration,
            kp=args.kp,
            kd=args.kd,
            ki=args.ki,
            sim_kp=args.sim_kp,
            sim_kv=args.sim_kv,
            acceleration=args.acceleration,
            max_torque=args.max_torque,
            torque_enabled=(not args.torque_off),
            update_rate=50.0,
            data_dict=real_data,
            start_time=global_start,
            is_real=True,
            request_state=True
        ))
    elif args.test == "step":
        asyncio.run(run_step_test(
            kos=KOS(args.ip),
            actuator_id=args.actuator_id,
            step_size=args.step_size,
            step_hold_time=args.step_hold_time,
            step_count=args.step_count,
            kp=args.kp,
            kd=args.kd,
            ki=args.ki,
            sim_kp=args.sim_kp,
            sim_kv=args.sim_kv,
            acceleration=args.acceleration,
            max_torque=args.max_torque,
            torque_enabled=(not args.torque_off),
            vel_limit=400.0,
            data_dict=real_data,
            start_time=global_start,
            sample_rate=args.sample_rate,
            is_real=True
        ))
    out_queue.put(real_data)



#############################
# MAIN (CLI + Orchestration)#
#############################
async def main():
    parser = argparse.ArgumentParser(
        description="ktune - CLI tool for actuator tests (sine or step) on both simulator and real robot."
    )
    parser.add_argument("--name", default="Zeroth01", help="Name For Plot titles")
    parser.add_argument("--sim_ip", default="127.0.0.1", help="Simulator KOS IP address (default=localhost)")
    parser.add_argument("--ip", default="192.168.42.1", help="Real robot KOS IP address (default=192.168.42.1)")
    parser.add_argument("--actuator-id", type=int, default=11, help="Actuator ID to test.")
    parser.add_argument("--test", choices=["sine", "step"], default="sine", help="Type of test to run.")

    # Sine test parameters
    parser.add_argument("--freq", type=float, default=1.0, help="Sine frequency (Hz)")
    parser.add_argument("--amp", type=float, default=5.0, help="Sine amplitude (degrees)")
    parser.add_argument("--duration", type=float, default=5.0, help="Sine test duration (seconds)")

    # Step test parameters
    parser.add_argument("--step-size", type=float, default=10.0, help="Step size (degrees)")
    parser.add_argument("--step-hold-time", type=float, default=3.0, help="Time to hold at step (seconds)")
    parser.add_argument("--step-count", type=int, default=2, help="Number of steps to take")

    # Simulation gains
    parser.add_argument("--sim-kp", type=float, default=24.0, help="Proportional gain")
    parser.add_argument("--sim-kv", type=float, default=0.75, help="Damping gain")

    # Actuator config
    parser.add_argument("--kp", type=float, default=20.0, help="Proportional gain")
    parser.add_argument("--kd", type=float, default=55.0, help="Derivative gain")
    parser.add_argument("--ki", type=float, default=0.01, help="Integral gain")
    parser.add_argument("--acceleration", type=float, default=2000.0, help="Acceleration (deg/s^2)")
    parser.add_argument("--max-torque", type=float, default=100.0, help="Max torque")
    parser.add_argument("--torque-off", action="store_true", help="Disable torque for test?")

    # Data logging
    parser.add_argument("--no-log", action="store_true", help="Do not record/plot data.")
    parser.add_argument("--log-duration-pad", type=float, default=2.0,
                        help="Pad (seconds) after motion ends to keep logging.")
    parser.add_argument("--sample-rate", type=float, default=50.0, help="Data collection rate (Hz).")

    args = parser.parse_args()

    # Prepare separate data dictionaries for simulator and real robot.
    sim_data = {
        "time": [],
        "position": [],
        "velocity": [],
        "cmd_time": [],
        "cmd_pos": [],
        "cmd_vel": []
    }
    real_data = {
        "time": [],
        "position": [],
        "velocity": [],
        "cmd_time": [],
        "cmd_pos": [],
        "cmd_vel": []
    }
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"Connecting to Simulator at {args.sim_ip} and Real robot at {args.ip}...")
    global_start = time.time()
    sim_queue = Queue()
    real_queue = Queue()

    sim_proc = Process(target=run_sim_test, args=(args, global_start, sim_queue))
    real_proc = Process(target=run_real_test, args=(args, global_start, real_queue))

    sim_proc.start()
    real_proc.start()

    sim_proc.join()
    real_proc.join()

    sim_data = sim_queue.get()
    real_data = real_queue.get()

        # Plotting both simulator and real data on the same plots.
    if not args.no_log:
        os.makedirs("plots", exist_ok=True)
        print(f"Saving plot data to plots/{args.test}_test_{now_str}.png")


        fig, axs = plt.subplots(2, 2, figsize=(14, 8), sharex=True)

        # Build a title string based on the test type.
        if args.test == "sine":
            title_str = (f"{args.name} -- Sine Wave Test -- Actuator {args.actuator_id}\n"
                        f"Freq: {args.freq} Hz, Amp: {args.amp}°, Cmd: 50Hz, Data: {args.sample_rate} Hz\n"
                        f"Sim Kp: {args.sim_kp} Kv: {args.sim_kv} | Real Kp: {args.kp} Kd: {args.kd} Ki: {args.ki}\n"
                        f"Acceleration: {args.acceleration:.0f} deg/s²")
        elif args.test == "step":
            # Construct the step sequence as used in the test:
            # Initial hold at 0°, then for each cycle: step up to step_size then step down to 0°
            vel = args.vel_limit if hasattr(args, "vel_limit") else 200.0
            steps_list = [(0.0, vel, args.step_hold_time)]
            for _ in range(args.step_count):
                steps_list.append((args.step_size, vel, args.step_hold_time))
                steps_list.append((0.0, vel, args.step_hold_time))
            
            # Compute overshoots using the collected time and position data.
            overshoots_sim = compute_step_overshoots(np.array(sim_data["time"]), np.array(sim_data["position"]), steps_list, window_duration=1.0)
            overshoots_real = compute_step_overshoots(np.array(real_data["time"]), np.array(real_data["position"]), steps_list, window_duration=1.0)
            max_overshoot_sim = max(overshoots_sim) if len(overshoots_sim) > 0 else 0.0
            max_overshoot_real = max(overshoots_real) if len(overshoots_real) > 0 else 0.0

            title_str = (
                f"{args.name} -- Step Test -- Actuator {args.actuator_id}\n"
                f"Step Size: {args.step_size}°, Hold: {args.step_hold_time}s, Count: {args.step_count}\n"
                f"Sim Kp: {args.sim_kp} Kv: {args.sim_kv} | Real Kp: {args.kp} Kd: {args.kd} Ki: {args.ki}\n"
                f"Overshoot - Sim: {max_overshoot_sim:.1f}%  Real: {max_overshoot_real:.1f}%\n"
                f"Acceleration: {args.acceleration:.0f} deg/s²"
            )
        else:
            title_str = f"{args.test.capitalize()} Test - Actuator {args.actuator_id}"
        fig.suptitle(title_str, fontsize=16)

        # Simulator subplots (left column)
        axs[0, 0].plot(sim_data["cmd_time"], sim_data["cmd_pos"], '--', color='black', linewidth=1.5, label='Sim Command Pos')
        axs[0, 0].plot(sim_data["time"], sim_data["position"], 'o-', color='blue', markersize=2, label='Sim Actual Pos')
        axs[0, 0].set_title("Sim - Position")
        axs[0, 0].set_ylabel("Position (deg)")
        axs[0, 0].legend()
        axs[0, 0].grid(True)

        if args.test == "sine":
            axs[1, 0].plot(sim_data["cmd_time"], sim_data["cmd_vel"], '--', color='black', linewidth=1.5, label='Sim Command Vel')
        axs[1, 0].plot(sim_data["time"], sim_data["velocity"], 'o-', color='blue', markersize=2, label='Sim Actual Vel')
        axs[1, 0].set_title("Sim - Velocity")
        axs[1, 0].set_xlabel("Time (s)")
        axs[1, 0].set_ylabel("Velocity (deg/s)")
        axs[1, 0].legend()
        axs[1, 0].grid(True)

        # Real Robot subplots (right column)
        axs[0, 1].plot(real_data["cmd_time"], real_data["cmd_pos"], '--', color='black', linewidth=1.5, label='Real Command Pos')
        axs[0, 1].plot(real_data["time"], real_data["position"], 's-', color='red', markersize=2, label='Real Actual Pos')
        axs[0, 1].set_title("Real - Position")
        axs[0, 1].set_ylabel("Position (deg)")
        axs[0, 1].legend()
        axs[0, 1].grid(True)

        if args.test == "sine":
            axs[1, 1].plot(real_data["cmd_time"], real_data["cmd_vel"], '--', color='black', linewidth=1.5, label='Real Command Vel')
        axs[1, 1].plot(real_data["time"], real_data["velocity"], 's-', color='red', markersize=2, label='Real Actual Vel')
        axs[1, 1].set_title("Real - Velocity")
        axs[1, 1].set_xlabel("Time (s)")
        axs[1, 1].set_ylabel("Velocity (deg/s)")
        axs[1, 1].legend()
        axs[1, 1].grid(True)

        plt.figtext(0.5, 0.02, "ktune", ha='center', va='center', fontsize=12)
        plt.tight_layout(rect=[0, 0.03, 1, 0.95])
        png_path = f"plots/{args.test}_comparison_{now_str}.png"
        plt.savefig(png_path)
        print(f"Saved comparison plot to {png_path}")
        plt.show()
        plt.close()


    print("Test complete.")


if __name__ == "__main__":
    asyncio.run(main())
