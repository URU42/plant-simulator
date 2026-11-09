"""RFCC 신호 모델 (설계 docs/superpowers/specs/2026-11-09-rfcc-simulator-design.md §4.2).

샘플 자료 생성기(rfcc-20261109/support/generate.py)의 식을 옮기되, 처음부터 끝까지 순차 필터였던 것을
측정 시각의 순수 함수로 바꿨다. 그래서 어느 구간을 잘라 요청해도, 재시작해도, 자정을 넘어도 같은 값이 나온다.

- 공통 성분: 공정 부하 L(t), 외기 A(t), 설비 부하 local_e(t) = L + 설비별 OU.
- 느린 성분(τ ≥ 500 s): 고정 기준 시각(KST 2026-01-01)부터 600초 격자의 정상 AR(1) 매듭을 선형 보간.
- 빠른 성분(τ ≤ 80 s): 날짜 블록을 (seed, seed_key, 성분, 날짜) 로 시드한 혁신 잡음과 잘린 지수 커널의 합성곱으로 만든다.
  블록 앞의 예열 구간은 전날 블록의 마지막 혁신값을 쓰므로 날짜 경계에서도 하나의 연속 필터와 같다.
- 온도: 강제항(부하·외기)을 시정수 240초로 지연시킨 뒤 느린 잡음을 더한다.
- 축 속도: 피구동 설비(driven_by)는 구동기 speed 태그의 시드와 기준값으로 같은 값을 만든다.
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np

from common.config_models import ConfigError, EquipmentConfig, PlantConfig, TagConfig
from common.sim_clock import KST
from simulator.signal_model import DAY_SEC, SignalModel, _seed_of

RFCC_EPOCH = datetime(2026, 1, 1, tzinfo=KST).timestamp()   # 느린 성분 매듭의 고정 기준 시각
KNOT_SEC = 600                  # 느린 성분 격자
KERNEL_TAUS = 5                 # AR(1)·지연 커널을 자르는 길이 (시정수의 배수)
BLOCK_CACHE_DAYS = 2            # 태그당 캐시하는 빠른 성분 블록 수
AMBIENT_TEMP_C = 25.0           # 정지 상태 온도
STOPPED_NOISE_RATIO = 0.3       # 정지 상태 잡음 비율
KST_OFFSET_SEC = int(KST.utcoffset(None).total_seconds())

TEMPERATURES = frozenset({"bearing_temp", "suction_temp", "discharge_temp", "oil_temp"})
SIGNAL_KINDS = TEMPERATURES | {"vibration", "shaft_vibration", "flow", "suction_pressure", "discharge_pressure",
                               "current", "speed", "axial_position", "oil_pressure", "steam_pressure"}
AIR_SHAFT_KINDS = frozenset({"blower", "expander"})     # 공기축: 속도 부하 계수 0.12, 나머지 0.25

# 빠른 성분의 시정수(초)와 흰 잡음 비율. 기본은 z = AR(s, 6) + N(0, 0.12·s)
_FAST = {kind: (80.0, 0.0) for kind in TEMPERATURES}
_FAST.update({"speed": (5.0, 0.0), "axial_position": (12.0, 0.0)})
_FAST_DEFAULT = (6.0, 0.12)

# 느린 성분 (sd, τ)
LOAD_OU = (0.004, 1400.0)
AMBIENT_OU = (0.08, 900.0)
LOCAL_OU = (0.002, 500.0)
VIBRATION_SLOW_OU_RATIO = (0.6, 700.0)      # sd = 0.6 × 잡음
LOAD_PERIOD_SEC = 110000.0
LOAD_STEP_AMP, LOAD_STEP_RAMP_SEC = 0.018, 2000.0
LOAD_STEP_HOURS = (4.0, 10.0)
TEMP_LAG_SEC = 240.0
# 온도 강제항 계수 (부하, 외기)
TEMP_FORCING = {"discharge_temp": (14.0, 0.6)}
TEMP_FORCING_DEFAULT = (3.0, 0.45)

# 시드 파생용 성분 번호
_C_LOAD, _C_LOAD_STEP, _C_AMBIENT, _C_LOCAL, _C_SLOW, _C_FAST = 10, 11, 12, 13, 14, 15


# ---------------------------------------------------------------------------
# 순수 함수 부품
# ---------------------------------------------------------------------------

def _kernel(tau_sec: float, step_sec: float) -> np.ndarray:
    a = math.exp(-step_sec / tau_sec)
    length = max(1, math.ceil(KERNEL_TAUS * tau_sec / step_sec))
    return a ** np.arange(length)


def ar1_series(rng: np.random.Generator, sd: float, tau_sec: float, step_sec: float, n: int) -> np.ndarray:
    """정상 AR(1) n 개. 잘린 지수 커널(길이 5τ)과 정규 잡음의 합성곱. 분산은 sd², 같은 rng 면 접두가 같다."""
    kernel = _kernel(tau_sec, step_sec)
    kernel *= sd / math.sqrt(float(np.square(kernel).sum()))
    noise = rng.normal(0.0, 1.0, n + len(kernel) - 1)
    return np.convolve(noise, kernel, "valid")


def lowpass(forcing: np.ndarray, tau_sec: float, step_sec: float) -> np.ndarray:
    """1차 지연 (DC 이득 1). 입력 앞 K−1 개는 예열용이라 출력 길이는 len(forcing) − K + 1."""
    kernel = _kernel(tau_sec, step_sec)
    return np.convolve(forcing, kernel / kernel.sum(), "valid")


class _SlowProcess:
    """고정 기준 시각부터 KNOT_SEC 격자의 정상 AR(1) 매듭. 요청 시각은 매듭 사이를 선형 보간한다."""

    def __init__(self, seed_parts: list[int], sd: float, tau_sec: float) -> None:
        self._seed_parts, self._sd, self._tau = seed_parts, sd, tau_sec
        self._knots = np.zeros(0)

    def at(self, epochs: np.ndarray) -> np.ndarray:
        pos = (epochs - RFCC_EPOCH) / KNOT_SEC
        needed = int(pos.max()) + 2
        if len(self._knots) < needed:      # 같은 시드로 더 길게 다시 만든다 (접두 불변)
            self._knots = ar1_series(np.random.default_rng(self._seed_parts), self._sd, self._tau, KNOT_SEC,
                                     max(needed, 2 * len(self._knots)))
        return np.interp(pos, np.arange(len(self._knots)), self._knots)


# ---------------------------------------------------------------------------
# 모델
# ---------------------------------------------------------------------------

class RfccSignalModel(SignalModel):
    """구성안 전체의 RFCC 정상 신호. values()/연관 규칙은 SignalModel 의 것을 그대로 쓴다."""

    def __init__(self, plant: PlantConfig, seed: int, sample_interval_sec: int) -> None:
        super().__init__(plant, seed, sample_interval_sec)
        self._seed = seed
        self._default_interval = sample_interval_sec
        self._tags = plant.tag_by_id()
        self._owner = plant.tag_owner()
        self._kind = {e.equip_id: plant.equipment_kind(e.equip_id) for e in plant.equipment}
        self._check_signals()
        self._load = _SlowProcess(_seed_of(seed, "load", _C_LOAD), *LOAD_OU)
        self._ambient = _SlowProcess(_seed_of(seed, "ambient", _C_AMBIENT), *AMBIENT_OU)
        self._local = {e.equip_id: _SlowProcess(_seed_of(seed, e.seed_key, _C_LOCAL), *LOCAL_OU) for e in plant.equipment}
        self._slow: dict[str, _SlowProcess] = {}                  # tag_id → 진동 느린 성분
        self._blocks: dict[str, dict[int, np.ndarray]] = {}       # tag_id → {날짜: 빠른 성분 블록}
        self._speed_source = self._resolve_speed_sources()
        self._formula = {
            "vibration": self._vibration, "shaft_vibration": self._vibration,
            "flow": self._flow, "suction_pressure": self._suction_pressure, "discharge_pressure": self._discharge_pressure,
            "current": self._current, "speed": self._speed, "axial_position": self._axial_position,
            "oil_pressure": self._oil_pressure, "steam_pressure": self._steam_pressure,
            **{kind: self._temperature for kind in TEMPERATURES},
        }

    # --- 검증 -------------------------------------------------------------
    def _check_signals(self) -> None:
        for tag in self._tags.values():
            if tag.signal not in SIGNAL_KINDS:
                raise ConfigError(f"tag {tag.tag_id}: unknown signal kind {tag.signal} for signal_model rfcc")

    def _resolve_speed_sources(self) -> dict[str, TagConfig]:
        """피구동 설비의 speed 태그 → 축을 공유하는 구동기(끝까지 따라간)의 speed 태그."""
        by_id = self.plant.equipment_by_id()
        speed_tag = {e.equip_id: next((t for t in e.tags if t.signal == "speed"), None) for e in self.plant.equipment}
        source: dict[str, TagConfig] = {}
        for e in self.plant.equipment:
            own = speed_tag[e.equip_id]
            if own is None:
                continue
            driver, seen = e, {e.equip_id}
            while driver.driven_by and speed_tag.get(driver.driven_by) is not None and driver.driven_by not in seen:
                driver = by_id[driver.driven_by]
                seen.add(driver.equip_id)
            shared = speed_tag[driver.equip_id]
            if shared.normal.base != own.normal.base:
                raise ConfigError(f"equipment {e.equip_id}: speed base {own.normal.base} differs from driver "
                                  f"{driver.equip_id} ({shared.normal.base}); directly coupled shafts share one speed")
            source[own.tag_id] = shared
        return source

    @staticmethod
    def _check_epochs(epochs: np.ndarray) -> None:
        if len(epochs) and epochs.min() < RFCC_EPOCH:
            raise ConfigError("rfcc signal model cannot generate values before 2026-01-01 (KST), its fixed origin")

    # --- 공개 인터페이스 -------------------------------------------------------
    def normal_values(self, tag_id: str, epochs: np.ndarray) -> np.ndarray:
        self._check_epochs(epochs)
        tag = self._tags[tag_id]
        return self._formula[tag.signal](tag, self._owner[tag_id], epochs)

    def stopped_values(self, tag_id: str, epochs: np.ndarray) -> np.ndarray:
        """정지 상태: 온도는 주변 온도, 축 위치는 기준값, 나머지는 0. 잡음은 작게 남긴다."""
        self._check_epochs(epochs)
        tag = self._tags[tag_id]
        rest = AMBIENT_TEMP_C if tag.signal in TEMPERATURES else tag.normal.base if tag.signal == "axial_position" else 0.0
        return rest + self._fast(tag, epochs) * STOPPED_NOISE_RATIO

    # --- 공통 성분 ---------------------------------------------------------
    def load(self, epochs: np.ndarray) -> np.ndarray:
        """공정 부하 L(t): 긴 주기 사인 + OU + 하루 한 번 완만한 부하 변경."""
        t = epochs - RFCC_EPOCH
        return 0.025 * np.sin(2 * np.pi * t / LOAD_PERIOD_SEC) + self._load.at(epochs) + self._load_step(epochs)

    def _load_step(self, epochs: np.ndarray) -> np.ndarray:
        """KST 날짜마다 시드로 정한 시각·방향·길이의 tanh 상승-하강. 자정에 걸치므로 앞뒤 날짜도 더한다."""
        days = (epochs + KST_OFFSET_SEC) // DAY_SEC
        out = np.zeros(len(epochs))
        for day in range(int(days.min()) - 1, int(days.max()) + 2):
            rng = np.random.default_rng(_seed_of(self._seed, "load_step", _C_LOAD_STEP, day))
            t0 = day * DAY_SEC - KST_OFFSET_SEC + rng.uniform(0, DAY_SEC)
            direction = 1.0 if rng.uniform() < 0.5 else -1.0
            length = rng.uniform(*LOAD_STEP_HOURS) * 3600
            out += LOAD_STEP_AMP * direction * (np.tanh((epochs - t0) / LOAD_STEP_RAMP_SEC)
                                                - np.tanh((epochs - t0 - length) / LOAD_STEP_RAMP_SEC)) / 2
        return out

    def ambient(self, epochs: np.ndarray) -> np.ndarray:
        """외기 A(t): 정오 무렵 최고인 하루 주기 + OU."""
        local_sec = (epochs + KST_OFFSET_SEC) % DAY_SEC
        return 2.2 * np.sin(2 * np.pi * (local_sec - 22000) / DAY_SEC) + self._ambient.at(epochs)

    def local(self, equip: EquipmentConfig, epochs: np.ndarray) -> np.ndarray:
        """설비 부하 local_e(t) = L(t) + 설비별 OU."""
        return self.load(epochs) + self._local[equip.equip_id].at(epochs)

    # --- 빠른 성분 (날짜 블록) ---------------------------------------------
    def _fast(self, tag: TagConfig, epochs: np.ndarray) -> np.ndarray:
        """태그의 빠른 잡음. 태그 주기 격자의 하루 블록에서 꺼낸다. 격자 사이 시각은 직전 격자 값."""
        interval = tag.interval_sec or self._default_interval
        days = epochs // DAY_SEC
        idx = (epochs % DAY_SEC) // interval
        out = np.empty(len(epochs))
        for day in np.unique(days):
            mask = days == day
            out[mask] = self._block(tag, int(day), interval)[idx[mask]]
        return out

    def _block(self, tag: TagConfig, day: int, interval: int) -> np.ndarray:
        """하루 블록 = AR(1) 잡음 (+ 흰 잡음). 예열은 전날 혁신값이라 경계에서 끊기지 않는다."""
        cache = self._blocks.setdefault(tag.tag_id, {})
        if day not in cache:
            tau, white = _FAST.get(tag.signal, _FAST_DEFAULT)
            n = DAY_SEC // interval
            kernel = _kernel(tau, interval)
            kernel *= tag.normal.noise_sd / math.sqrt(float(np.square(kernel).sum()))
            ar_prev, _ = self._innovations(tag, day - 1, n)
            ar_today, white_today = self._innovations(tag, day, n)
            warmup = ar_prev[n - (len(kernel) - 1):] if len(kernel) > 1 else ar_prev[:0]
            block = np.convolve(np.concatenate([warmup, ar_today]), kernel, "valid")
            if white:
                block = block + white * tag.normal.noise_sd * white_today
            cache[day] = block
            while len(cache) > BLOCK_CACHE_DAYS:
                cache.pop(next(iter(cache)))
        return cache[day]

    def _innovations(self, tag: TagConfig, day: int, n: int) -> tuple[np.ndarray, np.ndarray]:
        """(AR 혁신 n 개, 흰 잡음 n 개). 날짜·태그로 시드한 표준 정규."""
        rng = np.random.default_rng(_seed_of(self._seed, tag.seed_key, _C_FAST, day))
        return rng.normal(0.0, 1.0, n), rng.normal(0.0, 1.0, n)

    # --- 태그 종류별 식 -----------------------------------------------------
    def _vibration(self, tag: TagConfig, equip: EquipmentConfig, epochs: np.ndarray) -> np.ndarray:
        if tag.tag_id not in self._slow:
            ratio, tau = VIBRATION_SLOW_OU_RATIO
            self._slow[tag.tag_id] = _SlowProcess(_seed_of(self._seed, tag.seed_key, _C_SLOW), ratio * tag.normal.noise_sd, tau)
        b = tag.normal.base
        return b * (1 + 0.7 * self.local(equip, epochs)) + self._fast(tag, epochs) + self._slow[tag.tag_id].at(epochs)

    def _temperature(self, tag: TagConfig, equip: EquipmentConfig, epochs: np.ndarray) -> np.ndarray:
        """b + LP₂₄₀(k_load·local + k_amb·A) + 느린 잡음. 강제항은 태그 주기 격자에서 예열 구간을 붙여 평가한다."""
        k_load, k_amb = TEMP_FORCING.get(tag.signal, TEMP_FORCING_DEFAULT)
        step = tag.interval_sec or self._default_interval
        warmup = len(_kernel(TEMP_LAG_SEC, step)) - 1
        g0 = (int(epochs.min()) // step) * step - warmup * step
        grid = np.arange(g0, int(epochs.max()) + step, step, dtype=np.int64)
        forcing = k_load * self.local(equip, grid) + k_amb * self.ambient(grid)
        lagged = lowpass(forcing, TEMP_LAG_SEC, step)
        return tag.normal.base + np.interp(epochs, grid[warmup:], lagged) + self._fast(tag, epochs)

    def _flow(self, tag: TagConfig, equip: EquipmentConfig, epochs: np.ndarray) -> np.ndarray:
        return tag.normal.base * (1 + self.local(equip, epochs)) + self._fast(tag, epochs)

    def _suction_pressure(self, tag: TagConfig, equip: EquipmentConfig, epochs: np.ndarray) -> np.ndarray:
        return tag.normal.base - 0.12 * self.local(equip, epochs) + self._fast(tag, epochs)

    def _discharge_pressure(self, tag: TagConfig, equip: EquipmentConfig, epochs: np.ndarray) -> np.ndarray:
        """펌프: 양정이 부하에 따라 줄어든다 (같은 설비의 흡입 압력 기준값 필요). 그 외: 부하에 비례."""
        b = tag.normal.base
        suction = next((t for t in equip.tags if t.signal == "suction_pressure"), None)
        if self._kind[equip.equip_id] == "pump" and suction is not None:
            return b - (b - suction.normal.base) * 0.4 * self.local(equip, epochs) + self._fast(tag, epochs)
        return b * (1 + 0.2 * self.local(equip, epochs)) + self._fast(tag, epochs)

    def _current(self, tag: TagConfig, equip: EquipmentConfig, epochs: np.ndarray) -> np.ndarray:
        return tag.normal.base * (1 + 0.75 * self.local(equip, epochs)) + self._fast(tag, epochs)

    def _speed(self, tag: TagConfig, equip: EquipmentConfig, epochs: np.ndarray) -> np.ndarray:
        """축 속도는 공정 부하 L 에만 반응한다. 같은 축의 설비는 구동기 태그로 같은 값을 만든다."""
        shared = self._speed_source[tag.tag_id]
        k = 0.12 if self._kind[self._owner[shared.tag_id].equip_id] in AIR_SHAFT_KINDS else 0.25
        return shared.normal.base * (1 + k * self.load(epochs)) + self._fast(shared, epochs)

    def _axial_position(self, tag: TagConfig, equip: EquipmentConfig, epochs: np.ndarray) -> np.ndarray:
        return tag.normal.base + 0.005 * self.local(equip, epochs) + self._fast(tag, epochs)

    def _oil_pressure(self, tag: TagConfig, equip: EquipmentConfig, epochs: np.ndarray) -> np.ndarray:
        return tag.normal.base - 0.008 * self.ambient(epochs) + self._fast(tag, epochs)

    def _steam_pressure(self, tag: TagConfig, equip: EquipmentConfig, epochs: np.ndarray) -> np.ndarray:
        return tag.normal.base * (1 + 0.07 * self.local(equip, epochs)) + self._fast(tag, epochs)
