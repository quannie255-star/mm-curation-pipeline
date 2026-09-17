"""确定性合成 FHIR R4 评测语料（V4 α）。

为医疗模态算子/污染器提供带引用闭合的合成资源：Patient/Observation/Encounter/
MedicationRequest 四类，任何字段都程序生成——不含真实患者数据（假名池为通用
常见姓名样式，不指向真实个体，报告固定脚注）。

确定性约定：
- 随机只走 random.Random(seed)；id 顺序编号；时间从固定基准偏移；
  同 seed 输出逐字节一致（tests/test_fhir_synth.py 锁定）。
- 合法业务异常（不注入、不标注、评测计干净侧）：约 8% Observation 缺
  valueQuantity、约 10% Encounter 无 end（status=in-progress）、约 8% Patient
  无 telecom——防止算子靠「字段必须齐全」作弊拿召回。
- PHI 基线：name.given 脱敏为 ["*"]，telecom 只保留脱敏手机号
  +86-1**-****-****（真实样式姓名/完整手机号由污染器注入后交 phi_residual
  检出；邮箱属 PHI 风险项，基线一律不出现，干净语料零 PHI 模式命中）。
- 引用闭合：Observation/MedicationRequest 的 subject/encounter 全部指向存在
  的 Patient/Encounter——干净侧在 referential 算子上误杀 = 0 可归因。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from curation_eval import FHIRSample, Sample

N_PATIENT, N_ENCOUNTER = 100, 100
N_OBSERVATION, N_MEDICATION = 200, 100

# 通用常见姓名样式（姓氏/名各 20，组合 400；基线只落 family，given 脱敏为 "*"）
FAMILY_NAMES = (
    "张 王 李 赵 刘 陈 杨 黄 周 吴 徐 孙 马 朱 胡 郭 何 高 林 罗"
).split()
GIVEN_NAMES = (
    "伟 芳 娜 敏 静 丽 强 磊 军 洋 勇 艳 杰 娟 涛 明 超 秀 兰 霞"
).split()

# code_validity 内嵌码表（system URL -> (格式正则, 合法码集)），见 fhir_quality.py
ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10"
LOINC_SYSTEM = "http://loinc.org"
ATC_SYSTEM = "http://www.whocc.no/atc"

ICD10_CODES = (
    "E11.9 I10 J45.909 N18.9 K35.80 E78.5 I25.10 Z79.4 R51.9 M54.5 "
    "D64.9 E03.9 K21.9 J06.9 N39.0 R07.9 K29.70 E66.9 G43.909 I48.91 "
    "J18.9 N76.0 K76.9 R10.9 Z00.00 E55.9 D50.9 K59.00 M79.1 L23.9"
).split()

LOINC_CODES = (
    "8867-4 8480-6 8462-4 4548-4 2951-2 718-7 2160-0 1751-7 6298-4 2977-0 "
    "33914-3 17861-6 32207-3 26515-7 5902-2"
).split()

ATC_CODES = (
    "A10BA02 A10BH05 C09AA02 C10AA01 B01AC06 C07AB02 N02BE01 J01MA02 "
    "H03AA01 N05BA06"
).split()

UCUM_UNITS = ("mg/dL", "mmol/L", "mmHg", "{beats}/min", "%", "kg", "cm", "Cel", "mg", "mL")

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
_BIRTH_MIN, _BIRTH_MAX_DAYS = datetime(1940, 1, 1), datetime(2020, 12, 31)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _patient(i: int, rng: random.Random) -> dict:
    resource = {
        "resourceType": "Patient",
        "id": f"{i:06d}",
        "meta": {"lastUpdated": _iso(_EPOCH + timedelta(seconds=i))},
        "name": [
            {
                "family": FAMILY_NAMES[rng.randrange(len(FAMILY_NAMES))],
                "given": ["*"],  # 脱敏基线
            }
        ],
        "gender": rng.choice(["male", "female"]),
        "birthDate": (
            _BIRTH_MIN + timedelta(days=rng.randrange((_BIRTH_MAX_DAYS - _BIRTH_MIN).days))
        ).isoformat(),
        "address": [{"state": "某省", "city": "某市"}],  # 无详细街道（PHI 基线）
    }
    roll = rng.random()
    if roll < 0.08:
        pass  # 合法业务异常：无 telecom
    else:
        resource["telecom"] = [{"system": "phone", "value": "+86-1**-****-****"}]  # 脱敏
    return resource


def _encounter(j: int, n_patient: int, rng: random.Random) -> dict:
    start = _EPOCH + timedelta(hours=24 * (j % 90) + rng.randrange(24))
    resource = {
        "resourceType": "Encounter",
        "id": f"{j:06d}",
        "meta": {"lastUpdated": _iso(_EPOCH + timedelta(seconds=1000 + j))},
        "status": "finished",
        "class": {
            "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
            "code": rng.choice(["AMB", "IMP"]),
        },
        "subject": {"reference": f"Patient/{(j * 7) % n_patient:06d}"},
        "period": {"start": _iso(start)},
    }
    if rng.random() < 0.10:  # 合法业务异常：在诊无 end
        resource["status"] = "in-progress"
    else:
        resource["period"]["end"] = _iso(start + timedelta(hours=2 + rng.randrange(46)))
    return resource


def _observation(
    k: int,
    n_patient: int,
    enc_period: dict[str, tuple[datetime, datetime | None]],
    rng: random.Random,
) -> dict:
    enc_j = k % len(enc_period)
    enc_start, _enc_end = enc_period[f"{enc_j:06d}"]
    # 落在周期前 2 小时内（周期最短 2h，保证干净语料时间一致性零违例）
    effective = enc_start + timedelta(minutes=30 + (k * 17) % 80)
    resource = {
        "resourceType": "Observation",
        "id": f"{k:06d}",
        "meta": {"lastUpdated": _iso(_EPOCH + timedelta(seconds=2000 + k))},
        "status": "final",
        "code": {
            "coding": [
                {"system": LOINC_SYSTEM, "code": LOINC_CODES[k % len(LOINC_CODES)]}
            ]
        },
        "subject": {"reference": f"Patient/{k % n_patient:06d}"},
        "encounter": {"reference": f"Encounter/{enc_j:06d}"},
        "effectiveDateTime": _iso(effective),
    }
    if rng.random() >= 0.08:  # 约 8% 合法业务异常：缺 valueQuantity
        resource["valueQuantity"] = {
            "value": round(rng.uniform(1, 300), 1),
            "unit": UCUM_UNITS[rng.randrange(len(UCUM_UNITS))],
            "system": "http://unitsofmeasure.org",
        }
    return resource


def _medication_request(
    m: int,
    n_patient: int,
    enc_period: dict[str, tuple[datetime, datetime | None]],
    rng: random.Random,
) -> dict:
    enc_j = m % len(enc_period)
    enc_start, _enc_end = enc_period[f"{enc_j:06d}"]
    authored = enc_start + timedelta(minutes=30 + (m * 11) % 80)
    resource = {
        "resourceType": "MedicationRequest",
        "id": f"{m:06d}",
        "meta": {"lastUpdated": _iso(_EPOCH + timedelta(seconds=3000 + m))},
        "status": rng.choice(["active", "completed"]),
        "intent": "order",
        "medicationCodeableConcept": {
            "coding": [{"system": ATC_SYSTEM, "code": ATC_CODES[m % len(ATC_CODES)]}]
        },
        "subject": {"reference": f"Patient/{(m * 3) % n_patient:06d}"},
        "encounter": {"reference": f"Encounter/{enc_j:06d}"},
        "authoredOn": _iso(authored),
    }
    return resource


def generate_corpus(seed: int = 42, scale: float = 1.0) -> list[Sample]:
    """生成合成 FHIR R4 语料。scale 缩放各类型条数（冒烟/测试用），同 seed 逐字节一致。"""
    n_pat = max(1, round(N_PATIENT * scale))
    n_enc = max(1, round(N_ENCOUNTER * scale))
    n_obs = max(1, round(N_OBSERVATION * scale))
    n_med = max(1, round(N_MEDICATION * scale))
    rng = random.Random(seed)
    patients = [_patient(i, rng) for i in range(n_pat)]
    encounters = [_encounter(j, n_pat, rng) for j in range(n_enc)]
    enc_period = {
        e["id"]: (
            datetime.fromisoformat(e["period"]["start"]),
            datetime.fromisoformat(e["period"]["end"]) if "end" in e["period"] else None,
        )
        for e in encounters
    }
    samples = [FHIRSample.from_resource(r) for r in patients]
    samples += [FHIRSample.from_resource(r) for r in encounters]
    samples += [
        FHIRSample.from_resource(_observation(k, n_pat, enc_period, rng))
        for k in range(n_obs)
    ]
    samples += [
        FHIRSample.from_resource(_medication_request(m, n_pat, enc_period, rng))
        for m in range(n_med)
    ]
    return samples
