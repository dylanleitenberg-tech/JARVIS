"""Camera tracking: hands, body pose and face, on a worker thread.

MediaPipe runs synchronously and would block the event loop, so it lives on its
own thread and pushes results back through `Bus.publish_threadsafe`. The thread
also keeps the latest annotated JPEG in a slot the HUD's MJPEG endpoint drains,
which is why the feed never blocks tracking: a slow reader just misses frames.

The camera is only ever opened by `VisionWorker.start()`.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

import mediapipe as mp

mp_hands = mp.solutions.hands
mp_pose = mp.solutions.pose
mp_face = mp.solutions.face_detection

# Landmark indices we actually reason about.
WRIST, THUMB_CMC, THUMB_TIP = 0, 1, 4
INDEX_MCP, INDEX_PIP, INDEX_TIP = 5, 6, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_TIP = 9, 10, 12
RING_MCP, RING_PIP, RING_TIP = 13, 14, 16
PINKY_MCP, PINKY_PIP, PINKY_TIP = 17, 18, 20

FINGER_JOINTS = [  # (tip, pip, mcp) for index..pinky
    (INDEX_TIP, INDEX_PIP, INDEX_MCP),
    (MIDDLE_TIP, MIDDLE_PIP, MIDDLE_MCP),
    (RING_TIP, RING_PIP, RING_MCP),
    (PINKY_TIP, PINKY_PIP, PINKY_MCP),
]

# Pose landmarks worth sending to the HUD; the other 20 are noise on a torso shot.
POSE_KEEP = {0: "nose", 11: "l_shoulder", 12: "r_shoulder", 13: "l_elbow",
             14: "r_elbow", 15: "l_wrist", 16: "r_wrist", 23: "l_hip", 24: "r_hip"}

HAND_EDGES = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8),
              (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14), (14, 15),
              (15, 16), (13, 17), (17, 18), (18, 19), (19, 20), (0, 17)]

HUD_CYAN = (255, 212, 77)   # BGR of #4dd4ff
HUD_AMBER = (0, 176, 255)


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])



class HandOwner:
    """Which hands belong to the person at the keyboard.

    MediaPipe returns every hand it can see, and a second person leaning in was
    turning the model, arming gestures and clicking. The user is the hand that
    was already being followed: once a hand is taken as the owner it keeps that
    status by continuity (nearest palm, similar size) even if a bigger hand
    appears, and only after it has been gone for a second does the largest hand
    in view take over. A second hand counts as the same person's only when it
    is the other chirality, about the same size, and near the first.
    """

    HOLD_S = 1.0        # how long the owner is remembered after leaving the frame
    JUMP = 0.25         # max palm travel between frames, in frame widths
    PAIR_DIST = 0.45    # a second hand of the same person is this close to the first

    def __init__(self) -> None:
        self.primary: Optional[dict] = None     # palm, scale, label, t

    def mark(self, hands: List[dict], now: float) -> None:
        for h in hands:
            h["owner"] = False
        if not hands:
            return
        prim = None
        if self.primary is not None and now - self.primary["t"] <= self.HOLD_S:
            best, best_d = None, self.JUMP
            for h in hands:
                d = math.hypot(h["palm"][0] - self.primary["palm"][0],
                               h["palm"][1] - self.primary["palm"][1])
                ratio = h["scale"] / max(self.primary["scale"], 1e-4)
                if d < best_d and 0.5 <= ratio <= 2.0:
                    best, best_d = h, d
            prim = best
        if prim is None:
            if self.primary is not None and now - self.primary["t"] <= self.HOLD_S:
                return                          # the owner stepped out; nobody else drives yet
            prim = max(hands, key=lambda h: h["scale"])
        prim["owner"] = True
        self.primary = {"palm": list(prim["palm"]), "scale": prim["scale"],
                        "label": prim["label"], "t": now}
        for h in hands:
            if h is prim or h["label"] == prim["label"]:
                continue
            ratio = h["scale"] / max(prim["scale"], 1e-4)
            d = math.hypot(h["palm"][0] - prim["palm"][0], h["palm"][1] - prim["palm"][1])
            # Two hands of one person are the same distance from the lens, so
            # near the same size; a stranger's hand is usually not.
            if 0.75 <= ratio <= 1.35 and d < self.PAIR_DIST:
                h["owner"] = True

class VisionWorker(threading.Thread):
    daemon = True

    def __init__(self, config: dict, bus, on_frame=None):
        super().__init__(name="vision")
        self.cfg = config["vision"]
        self.bus = bus
        self.on_frame = on_frame
        # NOT `_stop`: threading.Thread has an internal _stop() method that
        # join() calls, and shadowing it makes join() raise
        # "'Event' object is not callable" — which aborted the whole
        # shutdown path the moment it started joining this thread.
        self._stopping = threading.Event()
        self._jpeg: Optional[bytes] = None
        self._jpeg_lock = threading.Lock()
        self.fps = 0.0
        self.error: Optional[str] = None
        self.running = False

    # ------------------------------------------------------------- lifecycle

    def stop(self, join: bool = False, timeout: float = 5.0) -> None:
        """Ask the worker to stop, and optionally wait until it really has.

        The camera is released in this thread's own `finally`. Setting the
        flag and walking away means the process can exit first, which leaves
        the capture device held and the camera light on until the OS reaps
        it. Anything shutting down for real wants join=True.
        """
        self._stopping.set()
        if join and self.is_alive():
            self.join(timeout=timeout)

    def latest_jpeg(self) -> Optional[bytes]:
        with self._jpeg_lock:
            return self._jpeg

    def run(self) -> None:  # thread entry
        cap = None
        try:
            cap = self._open_camera()
            if cap is None:
                return
            self.running = True
            self.bus.publish_threadsafe("vision_state", state="online",
                                        width=self.cfg["width"], height=self.cfg["height"])
            self._loop(cap)
        except Exception as exc:  # keep the rest of the system alive
            self.error = f"{exc.__class__.__name__}: {exc}"
            self.bus.publish_threadsafe("vision_state", state="error", error=self.error)
        finally:
            self.running = False
            if cap is not None:
                cap.release()
            self.bus.publish_threadsafe("vision_state", state="offline")

    def _open_camera(self):
        index = int(self.cfg["camera_index"])
        cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            self.error = (f"camera {index} would not open — check System Settings > "
                          f"Privacy & Security > Camera")
            self.bus.publish_threadsafe("vision_state", state="error", error=self.error)
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg["width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg["height"])
        cap.set(cv2.CAP_PROP_FPS, self.cfg["fps"])
        return cap

    # ------------------------------------------------------------- main loop

    def _loop(self, cap) -> None:
        det, track = self.cfg["min_detection_confidence"], self.cfg["min_tracking_confidence"]
        hands = mp_hands.Hands(
            max_num_hands=self.cfg["max_hands"], model_complexity=0,
            min_detection_confidence=det, min_tracking_confidence=track,
        ) if self.cfg["hands"] else None
        pose = mp_pose.Pose(
            model_complexity=0, smooth_landmarks=True,
            min_detection_confidence=det, min_tracking_confidence=track,
        ) if self.cfg["pose"] else None
        face = mp_face.FaceDetection(min_detection_confidence=det) if self.cfg["face"] else None

        last, smoothed_fps, pose_tick = time.time(), 0.0, 0
        try:
            while not self._stopping.is_set():
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.02)
                    continue
                if self.cfg["mirror"]:
                    frame = cv2.flip(frame, 1)
                h, w = frame.shape[:2]
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                rgb.flags.writeable = False

                hands_out: List[dict] = []
                if hands is not None:
                    result = hands.process(rgb)
                    if result.multi_hand_landmarks:
                        handedness = result.multi_handedness or []
                        for i, lm in enumerate(result.multi_hand_landmarks):
                            label = "right"
                            if i < len(handedness) and handedness[i].classification:
                                label = handedness[i].classification[0].label.lower()
                                if self.cfg["mirror"]:  # the flip swaps chirality back
                                    label = "left" if label == "right" else "right"
                            hands_out.append(self._describe_hand(lm.landmark, label))

                # Whose hands are these? Decided here, once, so the gesture
                # engine and the HUD's viewer agree on it.
                owner = getattr(self, "_owner", None)
                if owner is None:
                    owner = self._owner = HandOwner()
                owner.mark(hands_out, time.time())

                # Pose is the expensive model and body posture changes slowly,
                # so run it on every third frame.
                pose_out: Dict[str, dict] = {}
                pose_tick += 1
                if pose is not None and pose_tick % 3 == 0:
                    result = pose.process(rgb)
                    if result.pose_landmarks:
                        for idx, name in POSE_KEEP.items():
                            p = result.pose_landmarks.landmark[idx]
                            if p.visibility > 0.4:
                                pose_out[name] = {"x": round(p.x, 4), "y": round(p.y, 4),
                                                  "z": round(p.z, 3), "v": round(p.visibility, 2)}
                    self._last_pose = pose_out
                else:
                    pose_out = getattr(self, "_last_pose", {})

                face_out = None
                if face is not None and pose_tick % 3 == 1:
                    result = face.process(rgb)
                    if result.detections:
                        box = result.detections[0].location_data.relative_bounding_box
                        face_out = {"x": round(box.xmin, 4), "y": round(box.ymin, 4),
                                    "w": round(box.width, 4), "h": round(box.height, 4),
                                    "score": round(float(result.detections[0].score[0]), 2)}
                    self._last_face = face_out
                else:
                    face_out = getattr(self, "_last_face", None)

                now = time.time()
                dt = max(now - last, 1e-3)
                last = now
                smoothed_fps = 0.85 * smoothed_fps + 0.15 * (1.0 / dt)
                self.fps = round(smoothed_fps, 1)

                payload = {"hands": hands_out, "pose": pose_out, "face": face_out,
                           "fps": self.fps, "w": w, "h": h}
                self.bus.publish_threadsafe("vision", **payload)
                if self.on_frame is not None:
                    self.on_frame(payload)

                if self.cfg["stream_feed"]:
                    self._encode(frame, hands_out, pose_out, face_out)
        finally:
            for model in (hands, pose, face):
                if model is not None:
                    model.close()

    # ---------------------------------------------------------- hand geometry

    def _describe_hand(self, landmarks, label: str) -> dict:
        pts = [(round(p.x, 4), round(p.y, 4), round(p.z, 3)) for p in landmarks]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]

        # Hand scale: wrist to middle MCP. Every threshold is expressed as a
        # fraction of it, so gestures behave the same near and far from the lens.
        scale = max(_dist(pts[WRIST], pts[MIDDLE_MCP]), 1e-4)

        # The thumb can meet any of the four fingers, and each contact is a
        # distinct, reliable button — this is what makes finger-level control
        # possible rather than just whole-hand poses.
        pinches = [_dist(pts[THUMB_TIP], pts[tip]) / scale
                   for tip, _pip, _mcp in FINGER_JOINTS]
        # How far each fingertip sits from the wrist, in hand-scales. A pinch and
        # a closed fist both bring thumb and finger together; only the pinch
        # keeps that fingertip out away from the palm, so this tells them apart.
        reaches = [_dist(pts[tip], pts[WRIST]) / scale
                   for tip, _pip, _mcp in FINGER_JOINTS]
        pinch = pinches[0]
        index_reach = reaches[0]
        palm = ((pts[WRIST][0] + pts[INDEX_MCP][0] + pts[PINKY_MCP][0]) / 3.0,
                (pts[WRIST][1] + pts[INDEX_MCP][1] + pts[PINKY_MCP][1]) / 3.0)

        # A finger counts as extended when its tip is further from the wrist
        # than its PIP joint by a clear margin.
        extended = []
        for tip, pip, _mcp in FINGER_JOINTS:
            extended.append(_dist(pts[tip], pts[WRIST]) > _dist(pts[pip], pts[WRIST]) * 1.12)
        thumb_out = _dist(pts[THUMB_TIP], pts[PINKY_MCP]) > _dist(pts[THUMB_CMC], pts[PINKY_MCP]) * 1.05

        # Roll of the hand, used for dial-style continuous controls.
        dx = pts[INDEX_MCP][0] - pts[PINKY_MCP][0]
        dy = pts[INDEX_MCP][1] - pts[PINKY_MCP][1]
        roll = math.degrees(math.atan2(dy, dx))

        # Pointing direction of the index finger.
        pdx = pts[INDEX_TIP][0] - pts[INDEX_MCP][0]
        pdy = pts[INDEX_TIP][1] - pts[INDEX_MCP][1]

        return {
            "label": label,
            "points": pts,
            "palm": [round(palm[0], 4), round(palm[1], 4)],
            "index_tip": [pts[INDEX_TIP][0], pts[INDEX_TIP][1]],
            "thumb_tip": [pts[THUMB_TIP][0], pts[THUMB_TIP][1]],
            "pinch": round(pinch, 3),
            "pinches": [round(p, 3) for p in pinches],      # index, middle, ring, pinky
            "reaches": [round(r, 3) for r in reaches],
            "index_reach": round(index_reach, 3),
            "scale": round(scale, 4),
            "extended": extended,
            "n_extended": sum(extended),
            "thumb_out": thumb_out,
            "thumb_up": thumb_out and pts[THUMB_TIP][1] < pts[WRIST][1] - 0.6 * scale,
            "thumb_down": thumb_out and pts[THUMB_TIP][1] > pts[WRIST][1] + 0.6 * scale,
            "roll": round(roll, 1),
            "point_dir": [round(pdx, 3), round(pdy, 3)],
            "bbox": [round(min(xs), 4), round(min(ys), 4),
                     round(max(xs) - min(xs), 4), round(max(ys) - min(ys), 4)],
            "depth": round(-float(landmarks[WRIST].z), 3),
        }

    # -------------------------------------------------------------- HUD feed

    def _encode(self, frame, hands_out, pose_out, face_out) -> None:
        out_w = int(self.cfg["feed_width"])
        h, w = frame.shape[:2]
        out_h = int(h * out_w / w)
        small = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_AREA)

        # Desaturate and tint toward the HUD's cyan so the feed sits inside the
        # interface rather than looking like a webcam pasted on top of it.
        grey = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        tinted = np.zeros_like(small)
        tinted[:, :, 0] = np.clip(grey.astype(np.int16) + 36, 0, 255)   # B
        tinted[:, :, 1] = np.clip(grey.astype(np.int16) + 12, 0, 255)   # G
        tinted[:, :, 2] = (grey * 0.45).astype(np.uint8)                # R
        small = tinted

        for hand in hands_out:
            pts = [(int(p[0] * out_w), int(p[1] * out_h)) for p in hand["points"]]
            for a, b in HAND_EDGES:
                cv2.line(small, pts[a], pts[b], HUD_CYAN, 1, cv2.LINE_AA)
            for i, p in enumerate(pts):
                r = 3 if i in (THUMB_TIP, INDEX_TIP, MIDDLE_TIP, RING_TIP, PINKY_TIP) else 2
                cv2.circle(small, p, r, HUD_CYAN, -1, cv2.LINE_AA)
            x, y, bw, bh = hand["bbox"]
            self._bracket(small, int(x * out_w), int(y * out_h),
                          int(bw * out_w), int(bh * out_h), HUD_AMBER)

        if face_out:
            self._bracket(small, int(face_out["x"] * out_w), int(face_out["y"] * out_h),
                          int(face_out["w"] * out_w), int(face_out["h"] * out_h), HUD_CYAN)

        for name, p in (pose_out or {}).items():
            cv2.circle(small, (int(p["x"] * out_w), int(p["y"] * out_h)), 2, HUD_CYAN, -1, cv2.LINE_AA)

        ok, buf = cv2.imencode(".jpg", small,
                               [int(cv2.IMWRITE_JPEG_QUALITY), int(self.cfg["feed_quality"])])
        if ok:
            with self._jpeg_lock:
                self._jpeg = buf.tobytes()

    @staticmethod
    def _bracket(img, x: int, y: int, w: int, h: int, color) -> None:
        """Corner brackets — the HUD's targeting frame, not a plain rectangle."""
        c = max(6, min(w, h) // 4)
        for cx, sx in ((x, 1), (x + w, -1)):
            for cy, sy in ((y, 1), (y + h, -1)):
                cv2.line(img, (cx, cy), (cx + sx * c, cy), color, 1, cv2.LINE_AA)
                cv2.line(img, (cx, cy), (cx, cy + sy * c), color, 1, cv2.LINE_AA)
