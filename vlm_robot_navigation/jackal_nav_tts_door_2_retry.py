#!/usr/bin/env python3
# ROS 2 launcher for Nav2-based goal sequencing on a Clearpath Jackal.
# Features:
# - Goal sequencing with stop-specific prompts
# - Snapshot capture and blockage assessment
# - Annotated-image handling from the assessor
# - Door-button retry workflow with bounded retries
# - Deferred JSON handling on the ROS executor thread

import os
import sys
import time
import json
import uuid
import shlex
import threading
import subprocess
from pathlib import Path
from typing import Optional, Dict, List

import numpy as np
import cv2

import rclpy
from rclpy.node import Node
from rclpy.qos import (
 QoSProfile, QoSReliabilityPolicy, QoSHistoryPolicy, QoSDurabilityPolicy
)
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, Quaternion
from std_msgs.msg import String
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
from sensor_msgs.msg import LaserScan
import tf_transformations

# numpy compat (older ROS deps sometimes expect this)
if not hasattr(np, 'float'):
 np.float = float

LOG_PREFIX = "[JackalNavTTS]"

# Configuration

NAV_NAMESPACE = "/j100_0000"
SCAN_TOPIC = f"{NAV_NAMESPACE}/sensors/lidar2d_0/scan_filtered"

# Initial pose setup: (x, y, yaw) OR (x, y, qx, qy, qz, qw)
INITIAL_POSE = (0.607887240, 0.128126846, 0.0, 0.0, -0.0555200396, 0.998457573)

# Goals (quaternion form preserved)
GOAL_POSITIONS = [
 (9.5082305079399, 0.32567282705971, 0.0, 0.0, 0.0380704, 0.9992750568), # 1
 (15.20598446, -0.5373366988, 0.0, 0.0, -0.674258, 0.7384957), # 2
 (14.8499565, -4.1362481987, 0.0, 0.0, -0.72041467, 0.693543576), # 3
 None, None, None, None, None, None, None, None, None,
]

# Stop prompts per stop (launcher TTS)
STOP_PROMPTS = [
 ("Reached position one.", "Analyzing."), # 1
 ("Reached position two.", "Analyzing."), # 2
 ("Reached position 3.", ""), # 3
 ("", ""), ("", ""), ("", ""),
 ("", ""), ("", ""), ("", ""), ("", ""), ("", ""), ("", ""),
]

STARTUP_DELAY_SEC = 1.5
PAUSE_BETWEEN_STOPS_SEC = 5.0
MONITOR_PERIOD_SEC = 0.5
RESEND_ON_FAILURE = True
RESEND_DELAY_SEC = 2.0

# Blockage assessor control
BLOCKAGE_TRIGGER = "both" # "off" | "on_failure" | "on_recovery_threshold" | "both"
RECOVERY_THRESHOLD = 2
BLOCKAGE_COOLDOWN_SEC = 15.0
ASSESSOR_TIMEOUT_SEC = 45.0

# Front-arc blockage trigger
FRONT_FAN_DEG = 30.0 # +/- degrees for the front arc
FRONT_BLOCKED_RANGE = 0.67 # meters (door distance threshold)
TRIGGER_ON_FRONT_BLOCK = True

# Assessor process configuration
ASSESSOR_PYTHON = "/home/weim/venvs/depthai/bin/python"
ASSESSOR_PATH = str((Path(__file__).parent / "jackal_blockage_assessor.py").resolve())
BLOCKAGE_CMD = [ASSESSOR_PYTHON, "-u", ASSESSOR_PATH]

# Camera (the launcher owns the camera; assessor never touches it)
UVC_DEVICE_HINT = os.getenv("UVC_DEVICE_NAME", "arducam").strip() or "arducam"
CAP_DEFAULT_INDEX = 0
CAP_WIDTH = 1280
CAP_HEIGHT = 720
CAP_FOURCC = "MJPG"
CAP_WARMUP = 8

# Where we store snapshots (and assessor copies)
SNAP_DIR = Path.home() / "jackal_blockage"
SNAP_DIR.mkdir(parents=True, exist_ok=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# Button-press protocol configuration
BUTTON_POSE = (16.87569524, -1.325205254, 0.0, 0.0, -0.6669814856, 0.7450742901) # (x,y,qx,qy,qz,qw)
WAIT_AFTER_BUTTON_SEC = 10.0 # ⬅️ increased from 8s to 10s
BUTTON_MAX_RETRIES = 2 # ⬅️ cap retries at 2
BUTTON_MAX_ATTEMPTS = 1 + BUTTON_MAX_RETRIES # 1 initial + retries

# Retry triggers after resuming original course:
BUTTON_RETRY_WATCH_SEC = 6.0 # front-arc watch window right after resume
BUTTON_RETRY_COOLDOWN_SEC = 4.0 # min spacing between retry triggers
RETRY_TTS_DEBOUNCE_SEC = 2.0 # prevent duplicate "Retrying..." speech

class JackalNavTTS(Node):
 def __init__(self):
 super().__init__('jackal_nav_tts', automatically_declare_parameters_from_overrides=False)
 self.ns = NAV_NAMESPACE if NAV_NAMESPACE.startswith('/') else '/' + NAV_NAMESPACE

 # Select the camera index from the device hint
 self._uvc_index = self._choose_uvc_index_by_hint(UVC_DEVICE_HINT, default_index=CAP_DEFAULT_INDEX)
 self.get_logger().info(f"{LOG_PREFIX} UVC device hint: '{UVC_DEVICE_HINT}', chosen index={self._uvc_index}")

 # Cross-thread callback queue
 self._deferred_lock = threading.Lock()
 self._deferred_calls: List = []

 # Assessor state
 self._last_blockage_ts = 0.0
 self._blockage_proc = None
 self._assessor_threads: List[threading.Thread] = []
 self._assessor_start_time = 0.0
 self._assessor_stdout_buf = ""
 self._assessor_ran_this_stop = False

 # Snapshot state
 self._last_stop_image_path: Optional[str] = None
 self._last_stop_image_stop_idx: Optional[int] = None
 self._last_stop_image_ts = 0.0

 # Button-press state
 self._button_mode_active = False
 self._button_goal_pose: Optional[PoseStamped] = None
 self._button_resume_pose: Optional[PoseStamped] = None
 self._button_resume_stop_idx: Optional[int] = None
 self._button_attempts = 0
 self._button_watch_deadline = 0.0
 self._button_retry_armed = False
 self._button_retry_cooldown_until = 0.0
 self._last_retry_tts_ts = 0.0
 self._button_exhausted_announced = False # ⬅️ announce only once when attempts exhausted

 # Nav2 interface
 self.get_logger().info(f"{LOG_PREFIX} Initializing BasicNavigator (ns={self.ns})")
 self.nav = BasicNavigator(namespace=self.ns)
 try:
 self.nav.waitUntilNav2Active()
 self.get_logger().info(f"{LOG_PREFIX} Nav2 active.")
 except Exception as e:
 self.get_logger().warn(f"{LOG_PREFIX} waitUntilNav2Active raised: {e}")

 # -------------------------
 # Publishers and subscribers
 # -------------------------
 initpose_qos = QoSProfile(
 reliability=QoSReliabilityPolicy.RELIABLE,
 history=QoSHistoryPolicy.KEEP_LAST,
 depth=1,
 durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
 )
 self._initpose_topics = ["/initialpose", f"{self.ns}/initialpose"]
 self._init_pubs = [self.create_publisher(PoseWithCovarianceStamped, t, initpose_qos)
 for t in self._initpose_topics]
 self._init_published_at = 0.0
 self._init_retried = False
 self._amcl_has_nonzero = False

 self.amcl_pose_sub = self.create_subscription(
 PoseWithCovarianceStamped, f'{self.ns}/amcl_pose', self._amcl_pose_cb, 10
 )

 sensor_qos = QoSProfile(
 reliability=QoSReliabilityPolicy.BEST_EFFORT,
 history=QoSHistoryPolicy.KEEP_LAST,
 depth=5,
 durability=QoSDurabilityPolicy.VOLATILE,
 )
 self.scan_sub = self.create_subscription(LaserScan, SCAN_TOPIC, self._scan_cb, sensor_qos)
 self._front_min_range = np.inf
 self._front_last_stamp = 0.0
 self._front_idx_lo = None
 self._front_idx_hi = None
 self._front_triggered_this_stop = False

 # TTS
 self.tts_pub_block = self.create_publisher(String, 'tts/say_blocking', 10)
 self.tts_done_sub = self.create_subscription(String, 'tts/done', self._tts_done_cb, 10)
 self._pending_tts_ids = set()

 # Goal stops
 self.stop_indices, self.goal_poses = [], {}
 for i, entry in enumerate(GOAL_POSITIONS, start=1):
 pose = self._pose_from_entry(entry)
 if pose:
 self.goal_poses[i] = pose
 self.stop_indices.append(i)
 if not self.stop_indices:
 self.get_logger().error(f"{LOG_PREFIX} No valid stops configured.")
 raise SystemExit(1)

 # Stop prompts
 self.prompts = {i: ((STOP_PROMPTS[i-1][0] or "").strip(),
 (STOP_PROMPTS[i-1][1] or "").strip())
 for i in range(1, 13)}

 # Runtime state
 self.received_amcl_pose = False
 self.current_stop_idx: Optional[int] = None
 self.sent_goal = False
 self.mission_done = False
 self.last_recovery_count = 0

 # Timers
 self._startup_timer = None
 self._next_stop_timer = None
 self._retry_timer = None
 self._button_wait_timer = None

 # Initial pose setup
 if INITIAL_POSE and self._publish_initial_pose_generic(INITIAL_POSE):
 self.get_logger().info(f"{LOG_PREFIX} Initial pose sent (TRANSIENT_LOCAL) → "
 f"{', '.join(self._initpose_topics)}")
 else:
 self.get_logger().info(f"{LOG_PREFIX} ⏳ Waiting for AMCL pose...")

 # Periodic monitor
 self.monitor_timer = self.create_timer(MONITOR_PERIOD_SEC, self._monitor)

 # Runtime checks
 k = (OPENAI_API_KEY or os.getenv("OPENAI_API_KEY", "")).strip()
 if k:
 self.get_logger().info(f"{LOG_PREFIX} OPENAI_API_KEY set: ***{k[-6:]}")
 else:
 self.get_logger().warn(f"{LOG_PREFIX} OPENAI_API_KEY not set; assessor will fail.")
 if not Path(ASSESSOR_PYTHON).exists():
 self.get_logger().error(f"{LOG_PREFIX} Assessor Python not found: {ASSESSOR_PYTHON}. Update ASSESSOR_PYTHON.")
 if not Path(ASSESSOR_PATH).exists():
 self.get_logger().error(f"{LOG_PREFIX} Assessor script not found: {ASSESSOR_PATH}")

 # Camera thread state
 self._cap_lock = threading.Lock()
 self._cap_running = True
 self._cap_latest = None
 self._cap_latest_ts = 0.0
 self._cap_thread = threading.Thread(target=self._camera_thread_fn, daemon=True)
 self._cap_thread.start()

 # Helper methods

 def _post_to_executor(self, fn):
 """Queue a callable to be run on the ROS executor thread (drained in _monitor)."""
 with self._deferred_lock:
 self._deferred_calls.append(fn)

 def _arm_oneshot(self, attr_name: str, delay_sec: float, cb):
 """Create a one-shot timer once and reuse it safely (avoids wait-set churn)."""
 t = getattr(self, attr_name, None)
 if t is None:
 t = self.create_timer(max(0.0, delay_sec), cb)
 setattr(self, attr_name, t)
 t.cancel() # will be explicitly reset below
 else:
 t.cancel()
 try:
 t.timer_period_ns = int(max(0.0, delay_sec) * 1e9)
 except Exception:
 pass
 t.reset()

 def _choose_uvc_index_by_hint(self, hint: str, default_index: int = 0) -> int:
 """Find a /dev/videoX whose sysfs 'name' contains the hint."""
 try:
 for idx in range(0, 20):
 name_path = Path(f"/sys/class/video4linux/video{idx}/name")
 if name_path.exists():
 nm = name_path.read_text(errors="ignore").strip().lower()
 if hint.lower() in nm:
 return idx
 except Exception:
 pass
 return default_index

 def _pose_from_entry(self, entry, frame='map'):
 if entry is None:
 return None
 goal = PoseStamped()
 goal.header.frame_id = frame
 if len(entry) == 3:
 x, y, yaw = entry
 qx, qy, qz, qw = tf_transformations.quaternion_from_euler(0.0, 0.0, float(yaw))
 elif len(entry) == 6:
 x, y, qx, qy, qz, qw = entry
 else:
 self.get_logger().error(f"{LOG_PREFIX} Bad stop entry {entry}. Expect (x,y,yaw) or (x,y,qx,qy,qz,qw).")
 return None
 goal.pose.position.x = float(x)
 goal.pose.position.y = float(y)
 goal.pose.orientation = Quaternion(x=qx, y=qy, z=qz, w=qw)
 return goal

 def _wait_for_topic_publishers(self, topic: str, timeout_sec: float = 8.0) -> bool:
 t0 = time.time()
 while time.time() - t0 < timeout_sec:
 infos = self.get_publishers_info_by_topic(topic)
 if len(infos) > 0:
 return True
 rclpy.spin_once(self, timeout_sec=0.05)
 return False

 def _wait_for_any_initpose_sub(self, timeout_sec: float = 8.0) -> bool:
 t0 = time.time()
 while time.time() - t0 < timeout_sec:
 counts = [pub.get_subscription_count() for pub in self._init_pubs]
 if any(c > 0 for c in counts):
 self.get_logger().info(f"{LOG_PREFIX} initialpose subscribers: "
 f"{dict(zip(self._initpose_topics, counts))}")
 return True
 rclpy.spin_once(self, timeout_sec=0.05); time.sleep(0.05)
 self.get_logger().warn(f"{LOG_PREFIX} initialpose: no subscribers detected on "
 f"{', '.join(self._initpose_topics)} (publishing latched anyway).")
 return False

 def _publish_initpose_to_all(self, msg: PoseWithCovarianceStamped, repeats: int = 4, dt: float = 0.15):
 for _ in range(repeats):
 for pub in self._init_pubs:
 pub.publish(msg)
 rclpy.spin_once(self, timeout_sec=0.01); time.sleep(dt)
 self._init_published_at = time.monotonic()
 counts = [pub.get_subscription_count() for pub in self._init_pubs]
 self.get_logger().info(f"{LOG_PREFIX} initpose published → counts {dict(zip(self._initpose_topics, counts))}")

 def _publish_initial_pose_generic(self, init):
 try:
 if len(init) == 3:
 x, y, yaw = init
 qx, qy, qz, qw = tf_transformations.quaternion_from_euler(0.0, 0.0, float(yaw))
 elif len(init) == 6:
 x, y, qx, qy, qz, qw = init
 else:
 return False

 msg = PoseWithCovarianceStamped()
 msg.header.stamp = self.get_clock().now().to_msg()
 msg.header.frame_id = 'map'
 msg.pose.pose.position.x = float(x)
 msg.pose.pose.position.y = float(y)
 msg.pose.pose.position.z = 0.0
 msg.pose.pose.orientation = Quaternion(x=qx, y=qy, z=qz, w=qw)

 cov = [0.0] * 36
 cov[0] = 0.25; cov[7] = 0.25; cov[35] = 0.1
 cov[14] = cov[21] = cov[28] = 1e6
 msg.pose.covariance = cov

 amcl_pose_topic = f'{self.ns}/amcl_pose'
 _ = self._wait_for_topic_publishers(amcl_pose_topic, timeout_sec=8.0)
 self._wait_for_any_initpose_sub(timeout_sec=8.0)

 self._publish_initpose_to_all(msg, repeats=4, dt=0.15)
 return True
 except Exception as e:
 self.get_logger().error(f"{LOG_PREFIX} Failed to publish initial pose: {e}")
 return False

 # LaserScan front-arc handling

 def _scan_cb(self, scan: LaserScan):
 if self._front_idx_lo is None:
 fan = np.deg2rad(FRONT_FAN_DEG)
 a_min = scan.angle_min
 a_inc = scan.angle_increment
 i_lo = int(np.floor(((-fan) - a_min) / a_inc))
 i_hi = int(np.ceil(((+fan) - a_min) / a_inc))
 self._front_idx_lo = max(0, min(len(scan.ranges) - 1, i_lo))
 self._front_idx_hi = max(0, min(len(scan.ranges) - 1, i_hi))
 if self._front_idx_lo > self._front_idx_hi:
 self._front_idx_lo, self._front_idx_hi = self._front_idx_hi, self._front_idx_lo

 rngs = scan.ranges[self._front_idx_lo:self._front_idx_hi+1] if self._front_idx_lo is not None else scan.ranges
 vals = [r for r in rngs if np.isfinite(r) and r > 0.0]
 self._front_min_range = min(vals) if vals else np.inf
 self._front_last_stamp = self.get_clock().now().nanoseconds * 1e-9

 def _front_blocked_now(self) -> bool:
 if (time.time() - self._front_last_stamp) > 1.0:
 return False
 return self._front_min_range <= float(FRONT_BLOCKED_RANGE)

 # Text-to-speech handling

 def _tts_done_cb(self, msg: String):
 try:
 payload = json.loads(msg.data)
 uid = str(payload.get("id") or "")
 if uid in self._pending_tts_ids:
 self._pending_tts_ids.remove(uid)
 self.get_logger().info(f"{LOG_PREFIX} TTS finished id={uid[:8]}")
 except Exception:
 pass

 def _say_async(self, text: str):
 t = (text or "").strip()
 if not t:
 return
 uid = str(uuid.uuid4())
 self._pending_tts_ids.add(uid)
 payload = json.dumps({"id": uid, "text": t})
 self.tts_pub_block.publish(String(data=payload))
 self.get_logger().info(f"{LOG_PREFIX} TTS queued: \"{t}\" (id={uid[:8]})")

 # Camera thread

 def _camera_thread_fn(self):
 cap = None
 try:
 try:
 cap = cv2.VideoCapture(self._uvc_index, cv2.CAP_V4L2)
 except Exception:
 cap = cv2.VideoCapture(self._uvc_index)
 if not cap or not cap.isOpened():
 self.get_logger().warn(f"{LOG_PREFIX} Camera index {self._uvc_index} could not be opened.")
 return
 try:
 cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAP_WIDTH)
 cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAP_HEIGHT)
 except Exception:
 pass
 try:
 cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*CAP_FOURCC))
 except Exception:
 pass
 # Camera warmup
 for _ in range(max(4, CAP_WARMUP)):
 cap.read()
 self.get_logger().info(f"{LOG_PREFIX} camera thread started (index={self._uvc_index}, {CAP_WIDTH}x{CAP_HEIGHT}, {CAP_FOURCC})")
 # Frame capture loop
 while getattr(self, "_cap_running", False):
 ok, frame = cap.read()
 if ok:
 with self._cap_lock:
 self._cap_latest = frame
 self._cap_latest_ts = time.time()
 time.sleep(0.05)
 finally:
 try:
 if cap is not None:
 cap.release()
 except Exception:
 pass

 def _capture_stop_image(self, stop_idx: int) -> Optional[str]:
 """Save most recent frame from the camera thread right after we reach a stop."""
 frame = None
 try:
 with self._cap_lock:
 if self._cap_latest is not None:
 frame = self._cap_latest.copy()
 except Exception:
 frame = None

 if frame is None:
 self.get_logger().warn(f"{LOG_PREFIX} Snapshot capture failed (no frame in camera thread).")
 return None

 out = SNAP_DIR / f"stop_{stop_idx}_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
 try:
 cv2.imwrite(str(out), frame)
 self.get_logger().info(f"{LOG_PREFIX} Saved stop snapshot (thread) → {out}")
 return str(out)
 except Exception as e:
 self.get_logger().warn(f"{LOG_PREFIX} Snapshot write failed (thread): {e}")
 return None

 def _grab_live_snapshot_for_assessor(self) -> Optional[str]:
 """Save a fresh frame from the camera thread as a JPEG for the assessor."""
 try:
 for _ in range(10):
 with self._cap_lock:
 frame = None if self._cap_latest is None else self._cap_latest.copy()
 ts = float(self._cap_latest_ts or 0.0)
 if frame is not None and (time.time() - ts) <= 1.0:
 out = SNAP_DIR / f"assess_live_{self.current_stop_idx or 'na'}_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
 cv2.imwrite(str(out), frame)
 self.get_logger().info(f"{LOG_PREFIX} Using live snapshot from camera thread for assessor → {out}")
 return str(out)
 time.sleep(0.05)
 self.get_logger().warn(f"{LOG_PREFIX} Live snapshot unavailable (no fresh frame).")
 return None
 except Exception as e:
 self.get_logger().warn(f"{LOG_PREFIX} Live snapshot error: {e}")
 return None

 # AMCL and timer handling

 def _amcl_pose_cb(self, m: PoseWithCovarianceStamped):
 px, py = m.pose.pose.position.x, m.pose.pose.position.y
 qz, qw = m.pose.pose.orientation.z, m.pose.pose.orientation.w
 if abs(px) > 1e-3 or abs(py) > 1e-3 or abs(qz) > 1e-3 or abs(qw - 1.0) > 1e-3:
 if not self.received_amcl_pose:
 self.received_amcl_pose = True
 self.get_logger().info(f"{LOG_PREFIX} AMCL pose received; arming startup timer.")
 self._arm_startup_timer()
 self._amcl_has_nonzero = True

 def _arm_startup_timer(self):
 self._arm_oneshot("_startup_timer", STARTUP_DELAY_SEC, self._send_first_stop_once)

 def _send_first_stop_once(self):
 if self._startup_timer:
 self._startup_timer.cancel()
 self.current_stop_idx = self.stop_indices[0]
 self._send_goal_for_current_stop()

 def _arm_next_stop_timer(self):
 self._arm_oneshot("_next_stop_timer", PAUSE_BETWEEN_STOPS_SEC, self._advance_and_send_once)

 def _advance_and_send_once(self):
 if self._next_stop_timer:
 self._next_stop_timer.cancel()
 cur_i = self.stop_indices.index(self.current_stop_idx) if self.current_stop_idx in self.stop_indices else -1
 if cur_i + 1 < len(self.stop_indices):
 self.current_stop_idx = self.stop_indices[cur_i + 1]
 self._send_goal_for_current_stop()
 else:
 self.mission_done = True
 self.get_logger().info(f"{LOG_PREFIX} Mission complete.")

 def _arm_retry_timer(self):
 self._arm_oneshot("_retry_timer", RESEND_DELAY_SEC, self._retry_current_once)

 def _retry_current_once(self):
 if self._retry_timer:
 self._retry_timer.cancel()
 if not self.mission_done and self.current_stop_idx is not None:
 self.get_logger().info(f"{LOG_PREFIX} Retrying Stop {self.current_stop_idx}...")
 self._send_goal_for_current_stop()

 # Blockage assessor integration

 def _get_blockage_env(self) -> dict:
 env = os.environ.copy()
 key = (OPENAI_API_KEY or os.getenv("OPENAI_API_KEY", "")).strip()
 if key:
 env["OPENAI_API_KEY"] = key
 self.get_logger().info(f"{LOG_PREFIX} Passing OPENAI_API_KEY to assessor: ***{key[-6:]}")
 else:
 self.get_logger().warn(f"{LOG_PREFIX} OPENAI_API_KEY missing; assessor may fail.")
 env["NAV_NAMESPACE"] = NAV_NAMESPACE
 return env

 def _build_blockage_args(self, reason: str) -> Optional[List[str]]:
 """Always provide the assessor a JPEG path (never the raw UVC index)."""
 base = ["--speak", "none"] # launcher handles all TTS

 if (self._last_stop_image_path and Path(self._last_stop_image_path).exists()
 and self._last_stop_image_stop_idx == self.current_stop_idx
 and (time.time() - self._last_stop_image_ts) < 300.0):
 self.get_logger().info(
 f"{LOG_PREFIX} Using stop snapshot for assessor "
 f"(stop={self._last_stop_image_stop_idx} → {self._last_stop_image_path})"
 )
 return ["--image", self._last_stop_image_path] + base

 live = self._grab_live_snapshot_for_assessor()
 if live:
 return ["--image", live] + base

 self.get_logger().error(f"{LOG_PREFIX} Assessor skipped: no image available (camera thread stale/unavailable).")
 return None

 def _stream_pipe(self, pipe, is_err: bool):
 try:
 for line in iter(pipe.readline, ''):
 line = line.rstrip()
 if not line:
 continue
 if is_err:
 self.get_logger().info(f"{LOG_PREFIX} Assessor: {line}")
 else:
 self._assessor_stdout_buf += (line + "\n")
 try:
 start = self._assessor_stdout_buf.find("{")
 end = self._assessor_stdout_buf.rfind("}") + 1
 if start >= 0 and end > start:
 raw = self._assessor_stdout_buf[start:end]
 js = json.loads(raw)
 self._assessor_stdout_buf = ""
 self._post_to_executor(lambda js=js: self._handle_assessor_json(js))
 except Exception:
 pass
 except Exception as e:
 self.get_logger().warn(f"{LOG_PREFIX} Assessor pipe stream error: {e}")

 def _button_detected_from_json(self, js: Dict) -> bool:
 act = (js.get("Recommended Action") or "").strip().lower()
 obj = (js.get("Interaction Object") or "").strip().lower()
 return (("button" in obj and obj != "none") or (act in {"press", "push"}))

 def _save_boxed_copy_if_available(self, out_img_url: str):
 """Copy assessor annotated file:// URL into SNAP_DIR with _boxed.jpg suffix."""
 try:
 if not out_img_url:
 return
 p = out_img_url
 if p.startswith("file://"):
 p = p[len("file://"):]
 src = Path(p)
 if not src.exists():
 return
 base = src.stem
 dst = SNAP_DIR / f"{base}_boxed.jpg"
 data = cv2.imread(str(src))
 if data is not None:
 cv2.imwrite(str(dst), data)
 self.get_logger().info(f"{LOG_PREFIX} Saved boxed variant → {dst}")
 except Exception as e:
 self.get_logger().warn(f"{LOG_PREFIX} boxed copy error: {e}")

 def _handle_assessor_json(self, js: Dict):
 """(Runs on ROS executor thread) Speak lines; start button protocol if indicated."""
 obs = js.get("Obstacle", "unknown")
 obj = js.get("Interaction Object", "none")
 act = js.get("Recommended Action", "none")
 out_img = js.get("Output Image", "")

 self.get_logger().info(f"{LOG_PREFIX} Assessor JSON → Obstacle='{obs}', Interaction='{obj}', Action='{act}'")
 if out_img:
 self.get_logger().info(f"{LOG_PREFIX} Assessor annotated image: {out_img}")
 self._save_boxed_copy_if_available(out_img)

 for l in [f"Obstacle: {obs}.", f"Interaction Object: {obj}.", f"Recommended Action: {act}"]:
 self._say_async(l)

 if self._button_detected_from_json(js):
 self._say_async("Button detected, initiating button press sequence.")
 self._start_button_protocol()
 else:
 self._say_async("No button detected. Please open the door so that I may pass through.")

 def _start_assessor(self, reason: str):
 if not Path(ASSESSOR_PYTHON).exists():
 self.get_logger().error(f"{LOG_PREFIX} Assessor Python not found: {ASSESSOR_PYTHON}")
 return
 if not Path(ASSESSOR_PATH).exists():
 self.get_logger().error(f"{LOG_PREFIX} Assessor script not found: {ASSESSOR_PATH}")
 return

 built = self._build_blockage_args(reason)
 if not built:
 return

 full_cmd = BLOCKAGE_CMD + built
 env = self._get_blockage_env()

 try:
 self._blockage_proc = subprocess.Popen(
 full_cmd, env=env, cwd=str(Path(ASSESSOR_PATH).parent),
 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
 )
 self._assessor_start_time = time.monotonic()

 t_out = threading.Thread(target=self._stream_pipe, args=(self._blockage_proc.stdout, False), daemon=True)
 t_err = threading.Thread(target=self._stream_pipe, args=(self._blockage_proc.stderr, True), daemon=True)
 t_out.start(); t_err.start()
 self._assessor_threads = [t_out, t_err]

 self._last_blockage_ts = time.monotonic()
 self._assessor_ran_this_stop = True
 self.get_logger().warn(f"{LOG_PREFIX} Invoked blockage assessor (reason={reason}): {shlex.join(full_cmd)}")
 except Exception as e:
 self.get_logger().error(f"{LOG_PREFIX} Failed to start blockage assessor: {e}")

 def _maybe_invoke_blockage_assessor(self, reason: str):
 if getattr(self, '_assessor_ran_this_stop', False):
 return
 trig = (BLOCKAGE_TRIGGER or "").strip().lower()
 if trig == 'off':
 return
 if reason == 'failure' and trig not in ('on_failure', 'both'):
 return
 if reason == 'recovery' and trig not in ('on_recovery_threshold', 'both'):
 return
 if reason == 'front_blocked' and trig not in ('both', 'on_recovery_threshold', 'on_failure', 'front_only'):
 pass
 if (time.monotonic() - self._last_blockage_ts) < max(0.0, float(BLOCKAGE_COOLDOWN_SEC)):
 return
 if self._blockage_proc is not None and self._blockage_proc.poll() is None:
 return
 self._start_assessor(reason)

 def _check_assessor_timeout(self):
 if self._blockage_proc is None:
 return
 if self._blockage_proc.poll() is not None:
 self._blockage_proc = None
 return
 if (time.monotonic() - self._assessor_start_time) > float(ASSESSOR_TIMEOUT_SEC):
 try:
 self._blockage_proc.kill()
 self.get_logger().warn(f"{LOG_PREFIX} Assessor exceeded {ASSESSOR_TIMEOUT_SEC}s and was killed.")
 except Exception:
 pass
 self._blockage_proc = None

 # Button-press protocol

 def _start_button_protocol(self):
 """Cancel current goal, go to BUTTON_POSE, wait, then resume original stop (with retries)."""
 if self._button_mode_active:
 self.get_logger().info(f"{LOG_PREFIX} Button-press protocol already active; ignoring re-trigger.")
 return
 if self.current_stop_idx is None or self.current_stop_idx not in self.goal_poses:
 self.get_logger().warn(f"{LOG_PREFIX} Button-press requested but no current stop; ignoring.")
 return
 if self._button_attempts >= int(BUTTON_MAX_ATTEMPTS):
 self.get_logger().warn(f"{LOG_PREFIX} Button attempts exhausted ({self._button_attempts}); skipping.")
 if not self._button_exhausted_announced:
 self._button_exhausted_announced = True
 self._say_async("Button press unsuccessful, please open the door so that I may pass through.")
 return

 # Disable retry triggers while running the button routine
 self._button_retry_armed = False

 # Cancel timers that should not fire during button handling
 if self._next_stop_timer:
 self._next_stop_timer.cancel()
 if self._retry_timer:
 self._retry_timer.cancel()

 button_pose = self._pose_from_entry(BUTTON_POSE)
 if not button_pose:
 self.get_logger().error(f"{LOG_PREFIX} Button pose invalid; aborting button-press protocol.")
 return
 self._button_goal_pose = button_pose
 self._button_resume_stop_idx = int(self.current_stop_idx)
 self._button_resume_pose = self.goal_poses[self._button_resume_stop_idx]

 # Cancel the current task if possible
 try:
 self.nav.cancelTask()
 except Exception:
 pass
 time.sleep(0.05)

 # Navigate to the button pose
 try:
 self._button_attempts += 1
 self._button_mode_active = True
 button_pose.header.stamp = self.get_clock().now().to_msg()
 self.nav.goToPose(button_pose)
 self.sent_goal = True
 self.last_recovery_count = 0
 self.get_logger().info(f"{LOG_PREFIX} Button-press protocol START → navigating to button pose (attempt {self._button_attempts}/{BUTTON_MAX_ATTEMPTS}).")
 except Exception as e:
 self._button_mode_active = False
 self.get_logger().error(f"{LOG_PREFIX} Failed to send button goal: {e}")

 def _schedule_button_wait_and_resume(self):
 """Called after reaching the button pose; waits N seconds then resumes original stop."""
 wait = max(0.0, float(WAIT_AFTER_BUTTON_SEC))
 self.get_logger().info(f"{LOG_PREFIX} Button-press reached; waiting {wait:.1f}s before resuming Stop {self._button_resume_stop_idx}.")
 self._say_async(f"Button reached. Waiting {int(wait)} seconds.")
 self._arm_oneshot("_button_wait_timer", wait, self._resume_after_button_once)

 def _resume_after_button_once(self):
 if self._button_wait_timer:
 self._button_wait_timer.cancel()
 if not self._button_mode_active:
 return

 if self._button_resume_pose is None or self._button_resume_stop_idx is None:
 self.get_logger().warn(f"{LOG_PREFIX} Missing resume pose/idx; ending button mode without resume.")
 self._button_mode_active = False
 return
 try:
 try:
 self.nav.cancelTask()
 except Exception:
 pass
 time.sleep(0.05)

 pose = self._button_resume_pose
 pose.header.stamp = self.get_clock().now().to_msg()
 self.nav.goToPose(pose)
 self.sent_goal = True
 self.last_recovery_count = 0

 # Re-arm retry triggers after resuming:
 # - Any planner recovery while armed
 # - Front-arc blockage during the retry watch window
 self._button_watch_deadline = time.time() + float(BUTTON_RETRY_WATCH_SEC)
 self._button_retry_armed = True
 self._button_retry_cooldown_until = time.time() # allow immediate trigger
 self.get_logger().info(f"{LOG_PREFIX} Resuming original goal → Stop {self._button_resume_stop_idx}. (retry armed)")
 self._say_async("Resuming original course.")
 except Exception as e:
 self.get_logger().error(f"{LOG_PREFIX} Failed to resume original goal: {e}")
 finally:
 # Exit immediate button mode; the monitor handles later retries
 self._button_mode_active = False

 def _maybe_retry_button(self, reason: str):
 """Centralized guard to trigger a button retry (cooldown, attempt caps, and arming)."""
 if self._button_mode_active:
 return
 if not self._button_retry_armed:
 return
 if self._button_attempts >= int(BUTTON_MAX_ATTEMPTS):
 self.get_logger().warn(f"{LOG_PREFIX} Retry skipped: attempts exhausted.")
 self._button_retry_armed = False
 if not self._button_exhausted_announced:
 self._button_exhausted_announced = True
 self._say_async("Button press unsuccessful, please open the door so that I may pass through.")
 return
 if self.current_stop_idx != self._button_resume_stop_idx:
 # Disarm retries if the robot is no longer pursuing the same stop
 self._button_retry_armed = False
 return
 now = time.time()
 if now < self._button_retry_cooldown_until:
 return

 self._button_retry_cooldown_until = now + float(BUTTON_RETRY_COOLDOWN_SEC)
 self._button_retry_armed = False

 # Debounce the retry announcement
 if (now - self._last_retry_tts_ts) >= float(RETRY_TTS_DEBOUNCE_SEC):
 self._last_retry_tts_ts = now
 self._say_async("Retrying button press.")

 self.get_logger().warn(f"{LOG_PREFIX} Retrying button press (reason={reason}).")
 self._start_button_protocol()

 # Navigation and monitor loop

 def _send_goal_for_current_stop(self):
 if self.current_stop_idx is None:
 return
 pose = self.goal_poses.get(self.current_stop_idx)
 if not pose:
 self.get_logger().warn(f"{LOG_PREFIX} Stop {self.current_stop_idx} has no pose; skipping.")
 self._advance_and_send_once()
 return
 pose.header.stamp = self.get_clock().now().to_msg()
 self._front_triggered_this_stop = False
 self._assessor_ran_this_stop = False # reset once-per-stop gate
 self.get_logger().info(f"{LOG_PREFIX} Sending goal → Stop {self.current_stop_idx} (ns={self.ns})")
 try:
 self.nav.goToPose(pose)
 self.sent_goal = True
 self.last_recovery_count = 0
 self.get_logger().info(f"{LOG_PREFIX} Navigating to goal (stop={self.current_stop_idx})...")
 except Exception as e:
 self.get_logger().error(f"{LOG_PREFIX} goToPose failed: {e}")
 self.sent_goal = False

 def _drain_deferred(self):
 while True:
 fn = None
 with self._deferred_lock:
 if self._deferred_calls:
 fn = self._deferred_calls.pop(0)
 if fn is None:
 break
 try:
 fn()
 except Exception as e:
 self.get_logger().warn(f"{LOG_PREFIX} deferred call error: {e}")

 def _monitor(self):
 # Run queued callbacks on the executor thread
 self._drain_deferred()

 self._check_assessor_timeout()

 # Re-publish the initial pose once if AMCL stays at zero
 if (not self._amcl_has_nonzero and self._init_published_at > 0.0 and
 not self._init_retried and (time.monotonic() - self._init_published_at) > 2.0):
 self.get_logger().warn(f"{LOG_PREFIX} AMCL still zero; re-sending initialpose once.")
 self._init_retried = True
 self._publish_initial_pose_generic(INITIAL_POSE)

 # Retry quickly if the front arc remains blocked after resuming
 if (not self._button_mode_active and self._button_attempts > 0 and
 self._button_attempts < int(BUTTON_MAX_ATTEMPTS) and
 self._button_retry_armed and time.time() < self._button_watch_deadline and
 self._front_blocked_now()):
 self._maybe_retry_button(reason="front_blocked")

 if self.sent_goal:
 # Navigation recovery feedback
 try:
 fb = self.nav.getFeedback()
 except Exception:
 fb = None

 if fb:
 recovs = getattr(fb, "number_of_recoveries", None)
 if recovs is not None and recovs > self.last_recovery_count:
 inc = recovs - self.last_recovery_count
 self.last_recovery_count = recovs
 self.get_logger().warn(
 f"{LOG_PREFIX} Planner recovery x{inc} at Stop {self.current_stop_idx}."
 )
 # Trigger a retry on any recovery event while armed
 if not self._button_mode_active and self._button_retry_armed:
 self._maybe_retry_button(reason="planner_recovery")

 # Optionally invoke the assessor at the general recovery threshold
 if not self._button_mode_active and self.last_recovery_count >= int(RECOVERY_THRESHOLD):
 self._maybe_invoke_blockage_assessor('recovery')

 # Invoke the assessor once per stop when the front arc becomes blocked
 if (TRIGGER_ON_FRONT_BLOCK and not self._front_triggered_this_stop and
 not self._button_mode_active and self._front_blocked_now()):
 self._front_triggered_this_stop = True
 self.get_logger().warn(
 f"{LOG_PREFIX} Front blocked ≤ {FRONT_BLOCKED_RANGE:.2f} m "
 f"(min={self._front_min_range:.2f} m). Invoking blockage assessor."
 )
 self._maybe_invoke_blockage_assessor('front_blocked')

 # Completion handling
 try:
 complete = self.nav.isTaskComplete()
 except Exception as e:
 self.get_logger().warn(f"{LOG_PREFIX} isTaskComplete() error: {e}")
 complete = False

 if complete:
 # Button leg reached → schedule wait then resume
 if self._button_mode_active:
 try:
 result = self.nav.getResult()
 except Exception:
 result = None
 self.get_logger().info(f"{LOG_PREFIX} Button goal complete (result={result}).")
 self.sent_goal = False
 self._schedule_button_wait_and_resume()
 return

 # Normal stop branch
 try:
 result = self.nav.getResult()
 except Exception as e:
 self.get_logger().warn(f"{LOG_PREFIX} getResult() error: {e}")
 result = None

 succeeded_flag = (
 result == TaskResult.SUCCEEDED or
 (hasattr(TaskResult, "SUCCEEDED") and hasattr(TaskResult.SUCCEEDED, "value") and result == TaskResult.SUCCEEDED.value) or
 result == 0
 )

 if succeeded_flag:
 self.get_logger().info(f"{LOG_PREFIX} Reached Stop {self.current_stop_idx}.")

 # Success: reset button retry state & exhausted announcement
 self._button_attempts = 0
 self._button_watch_deadline = 0.0
 self._button_retry_armed = False
 self._button_retry_cooldown_until = 0.0
 self._button_exhausted_announced = False

 # Capture the "post-goal" snapshot here (BEFORE next goal is sent)
 snap = self._capture_stop_image(self.current_stop_idx)
 if snap:
 self._last_stop_image_path = snap
 self._last_stop_image_stop_idx = self.current_stop_idx
 self._last_stop_image_ts = time.time()

 # Optional prompts (launcher TTS)
 p1, p2 = self.prompts.get(self.current_stop_idx, ("", ""))
 if p1: self._say_async(p1)
 if p2: self._say_async(p2)
 self.sent_goal = False

 if self.current_stop_idx != self.stop_indices[-1]:
 self.get_logger().info(f"{LOG_PREFIX} Scheduling next stop in {PAUSE_BETWEEN_STOPS_SEC:.1f}s...")
 self._arm_next_stop_timer()
 else:
 self.mission_done = True
 self.get_logger().info(f"{LOG_PREFIX} Mission complete.")
 else:
 self.get_logger().warn(f"{LOG_PREFIX} Could not reach Stop {self.current_stop_idx} (result={result}).")
 if not self._button_mode_active:
 self._maybe_invoke_blockage_assessor('failure')
 self.sent_goal = False
 if RESEND_ON_FAILURE and not self._button_mode_active:
 self.get_logger().info(f"{LOG_PREFIX} ⏳ Will retry in {RESEND_DELAY_SEC:.1f}s...")
 self._arm_retry_timer()
 return

 # No active goal → nothing else to do here

def main():
 rclpy.init()
 node = JackalNavTTS()
 try:
 rclpy.spin(node)
 finally:
 node._cap_running = False
 try:
 if node._cap_thread.is_alive():
 node._cap_thread.join(timeout=0.5)
 except Exception:
 pass
 node.destroy_node()
 rclpy.shutdown()


if __name__ == '__main__':
 main()
