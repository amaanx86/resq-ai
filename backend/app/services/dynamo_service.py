import json
import threading
import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key, Attr
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional, Tuple
from pydantic import BaseModel, TypeAdapter
from pydantic_core import to_jsonable_python
from app.config import settings
from app.models.schemas import (UserProfile, DoctorProfile, EmergencyCase, ChatMessage, AnalyticsSummary,
                                AdminProfile)

OPEN_CASE_STATUSES = ("ACTIVE", "DOCTOR_HANDOVER")
# How long get_doctor_by_phone reuses its scan for doctors saved without a phone pointer.
DOCTOR_PHONE_SCAN_SECONDS = 60
# Case fields that feed the monthly METRIC counters (see _case_metric_deltas).
_METRIC_FIELDS = {"esi_level", "matched_specialty", "assigned_doctor_name", "detected_language"}


def _to_item(model: BaseModel) -> Dict[str, Any]:
    """Model -> DynamoDB-safe dict (boto3 rejects Python floats; use Decimal)."""
    return json.loads(model.model_dump_json(), parse_float=Decimal)


def _to_value(value: Any) -> Any:
    """Single attribute value -> DynamoDB-safe value (floats become Decimal, models become maps)."""
    return json.loads(json.dumps(to_jsonable_python(value)), parse_float=Decimal)


@lru_cache(maxsize=None)
def _case_field_adapter(field_name: str) -> TypeAdapter:
    """Validator for one EmergencyCase field (update_case coerces values like the model would)."""
    return TypeAdapter(EmergencyCase.model_fields[field_name].annotation)


def _is_conditional_failure(e: Exception) -> bool:
    return isinstance(e, ClientError) and e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def phone_key(phone: Optional[str]) -> str:
    """Phone number as stored in lookup keys: '+' and digits only ("+92 300-1234567" -> "+923001234567")."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return f"+{digits}" if digits else ""


def _from_item(value: Any) -> Any:
    """DynamoDB item data -> plain Python (Decimal back to int/float)."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_item(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_item(v) for v in value]
    return value


def create_resq_table(client: Any, table_name: str) -> None:
    """Creates the single table with both GSIs. Only for Floci and tests: in AWS, Terraform owns it."""
    client.create_table(
        TableName=table_name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"}
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
            {"AttributeName": "GSI2PK", "AttributeType": "S"},
            {"AttributeName": "GSI2SK", "AttributeType": "S"}
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "GSI_SpecialtyActive",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"}
                ],
                "Projection": {"ProjectionType": "ALL"}
            },
            {
                "IndexName": "GSI_Analytics",
                "KeySchema": [
                    {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI2SK", "KeyType": "RANGE"}
                ],
                "Projection": {"ProjectionType": "ALL"}
            }
        ],
        BillingMode="PAY_PER_REQUEST"
    )


class DynamoDBService:
    def __init__(self):
        self.mock_mode = settings.MOCK_AWS
        self.table_name = settings.DYNAMODB_TABLE_NAME
        
        # In-memory store fallback
        self._mock_users: Dict[str, UserProfile] = {}
        self._mock_doctors: Dict[str, DoctorProfile] = {}
        self._mock_doctor_phones: Dict[str, str] = {}  # phone_key -> doctor_id (WA-0)
        self._mock_admins: Dict[str, AdminProfile] = {}  # ADMIN-2
        self._mock_cases: Dict[str, EmergencyCase] = {}
        self._mock_messages: Dict[str, List[ChatMessage]] = {}
        # Serializes in-memory conditional case updates (update_case_if), like DynamoDB would.
        self._case_lock = threading.Lock()
        # Same for the in-memory doctor lock (claim_doctor / release_doctor, OFFER-4).
        self._doctor_lock = threading.Lock()
        self.table = None
        self.dynamodb = None
        self.client = None

        if not self.mock_mode or settings.AWS_ENDPOINT_URL:
            try:
                boto_kwargs = {"region_name": settings.AWS_REGION}
                if settings.AWS_ENDPOINT_URL:
                    boto_kwargs["endpoint_url"] = settings.AWS_ENDPOINT_URL
                    boto_kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
                    boto_kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
                
                self.dynamodb = boto3.resource('dynamodb', **boto_kwargs)
                self.client = self.dynamodb.meta.client
                self._ensure_table_exists()
                self.table = self.dynamodb.Table(self.table_name)
                self.mock_mode = False
                print(f"[DynamoDB] Connected successfully to table '{self.table_name}' via {settings.AWS_ENDPOINT_URL or 'AWS Cloud'}")
            except Exception as e:
                if not settings.AWS_ENDPOINT_URL:
                    # Real AWS: an in-memory store per task would split cases between the API and
                    # worker and lose them on restart. Fail fast instead.
                    raise RuntimeError(
                        f"[DynamoDB] MOCK_AWS=False but table '{self.table_name}' is unavailable ({e}). "
                        "Check DYNAMODB_TABLE_NAME and the task role's DynamoDB permissions."
                    ) from e
                print(f"[DynamoDB] Warning: Could not connect to Floci, falling back to mock store: {e}")
                self.mock_mode = True
        
        # In-memory idempotency records (DynamoDB uses PK=IDEMPOTENCY#<key>, SK=LOCK)
        self._mock_idempotency: Dict[str, Dict[str, Any]] = {}
        # In-memory direct-call links (DynamoDB uses PK=DIRECTCALL#<token>, SK=LINK)
        self._mock_direct_call_links: Dict[str, Dict[str, Any]] = {}

        # Seed default doctors and sample patient (API process only; the worker sets SEED_DEMO_DATA=False)
        if settings.SEED_DEMO_DATA:
            self._seed_mock_data()

    def _ensure_table_exists(self):
        """Floci: creates ResQTable if missing. Real AWS: checks that the Terraform table is readable."""
        if not self.client:
            return
        if not settings.AWS_ENDPOINT_URL:
            # Real AWS: Terraform owns the table. Raises if it is missing or not readable.
            self.client.describe_table(TableName=self.table_name)
            return
        try:
            existing = self.client.list_tables().get("TableNames", [])
            if self.table_name not in existing:
                print(f"[DynamoDB] Table '{self.table_name}' not found. Auto-creating table on Floci/AWS...")
                create_resq_table(self.client, self.table_name)
                print(f"[DynamoDB] Created table '{self.table_name}' successfully.")
        except Exception as e:
            print(f"[DynamoDB] Error checking/creating table: {e}")

    # ---------------------------------------------------------------- DynamoDB helpers
    @property
    def _db(self) -> bool:
        """True when DynamoDB is the source of truth. Readers then return what DynamoDB says, even
        when it is empty; the in-memory mirror is used only if the DynamoDB call raises."""
        return not self.mock_mode and self.table is not None

    def _scan_all(self, **kwargs) -> List[Dict[str, Any]]:
        """Full scan, following LastEvaluatedKey (a single Scan page stops at 1 MB)."""
        items: List[Dict[str, Any]] = []
        while True:
            res = self.table.scan(**kwargs)
            items.extend(res.get("Items", []))
            if "LastEvaluatedKey" not in res:
                return items
            kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]

    def _query_all(self, **kwargs) -> List[Dict[str, Any]]:
        """Full query, following LastEvaluatedKey."""
        items: List[Dict[str, Any]] = []
        while True:
            res = self.table.query(**kwargs)
            items.extend(res.get("Items", []))
            if "LastEvaluatedKey" not in res:
                return items
            kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]

    def _put_if_absent(self, item: Dict[str, Any]) -> bool:
        """put_item that never overwrites an existing item. True if written."""
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
            return True
        except ClientError as e:
            if _is_conditional_failure(e):
                return False
            raise

    @staticmethod
    def _doctor_item(doctor: DoctorProfile) -> Dict[str, Any]:
        return {
            "PK": f"DOCTOR#{doctor.doctor_id}",
            "SK": "PROFILE",
            "GSI1PK": f"SPECIALTY#{doctor.specialty}",
            "GSI1SK": f"STATUS#{'ACTIVE' if doctor.is_active else 'INACTIVE'}",
            "data": _to_item(doctor),
        }

    @staticmethod
    def _doctor_phone_item(doctor: DoctorProfile) -> Optional[Dict[str, Any]]:
        """Pointer item for the doctor-by-phone lookup (WA-0); None without a usable phone."""
        key = phone_key(doctor.phone_number)
        if not key:
            return None
        return {"PK": f"DOCTORPHONE#{key}", "SK": "PROFILE", "doctor_id": doctor.doctor_id}

    @staticmethod
    def _user_item(user: UserProfile) -> Dict[str, Any]:
        return {"PK": f"USER#{user.phone_number}", "SK": "PROFILE", "data": _to_item(user),
                "registered_at": user.registered_at}

    def _seed_mock_data(self):
        """Populates initial doctors and patients in memory and DynamoDB"""
        cardio_doc = DoctorProfile(
            doctor_id="DOC-CARDIO-01",
            name="Dr. Sarah Khan, MD",
            phone_number="+923001234567",
            license_no="PMC-98214-C",
            specialty="Cardiology",
            bio="Senior Consultant Interventional Cardiologist with 15+ years managing acute myocardial infarction and cardiac emergencies.",
            is_active=True,
            cases_handled=18
        )
        neuro_doc = DoctorProfile(
            doctor_id="DOC-NEURO-02",
            name="Dr. Rajesh Sharma, MD",
            phone_number="+919811234567",
            license_no="MCI-44120-N",
            specialty="Neurology",
            bio="Emergency Neurologist specializing in acute ischemic stroke, seizures, and neurovascular intervention.",
            is_active=True,
            cases_handled=12
        )
        trauma_doc = DoctorProfile(
            doctor_id="DOC-TRAUMA-03",
            name="Dr. James Mitchell, FACS",
            phone_number="+14155552671",
            license_no="CA-MED-88124",
            specialty="Trauma",
            bio="Chief Trauma Surgeon, Advanced Trauma Life Support (ATLS) certified, mass casualty triage specialist.",
            is_active=True,
            cases_handled=24
        )
        pulmo_doc = DoctorProfile(
            doctor_id="DOC-PULMO-04",
            name="Dr. Ayesha Siddiqui, FCPS",
            phone_number="+923219876543",
            license_no="PMC-71239-P",
            specialty="Pulmonology",
            bio="Pulmonologist & Critical Care intensivist managing acute respiratory distress syndrome, anaphylaxis, and severe asthma.",
            is_active=True,
            cases_handled=9
        )
        # Seed only what is missing: a restart must not put an off-duty doctor back on duty, reset
        # cases_handled, or clear the demo patient's active_case_id.
        doctors = [cardio_doc, neuro_doc, trauma_doc, pulmo_doc]
        for doc in doctors:
            self._mock_doctors.setdefault(doc.doctor_id, doc)
            self._mock_doctor_phones.setdefault(phone_key(doc.phone_number), doc.doctor_id)

        # Seed default patient
        sample_user = UserProfile(
            phone_number="+923009988776",
            name="Zubair Ahmed",
            emergency_contact_name="Farhan Ahmed (Brother)",
            emergency_contact_phone="+923331122334",
            address="House 42, Block 5, Clifton, Karachi"
        )
        self._mock_users.setdefault(sample_user.phone_number, sample_user)

        # Connected to DynamoDB / Floci: conditional puts, so existing records are left untouched.
        if self._db:
            try:
                written = [doc.doctor_id for doc in doctors if self._put_if_absent(self._doctor_item(doc))]
                for doc in doctors:
                    self._put_if_absent(self._doctor_phone_item(doc))
                user_written = self._put_if_absent(self._user_item(sample_user))
                print(f"[DynamoDB] Seed: wrote doctors {written or 'none (all exist)'}, "
                      f"demo user {'written' if user_written else 'exists'}.")
            except Exception as e:
                print(f"[DynamoDB] Error syncing seed data to table: {e}")

    # ================= USER OPERATIONS =================
    def get_user(self, phone_number: str) -> Optional[UserProfile]:
        if self._db:
            try:
                item = self.table.get_item(Key={"PK": f"USER#{phone_number}", "SK": "PROFILE"}).get("Item")
                return UserProfile(**_from_item(item["data"])) if item else None
            except Exception as e:
                print(f"[DynamoDB] Error fetching user (using the in-memory mirror): {e}")
        return self._mock_users.get(phone_number)

    def save_user(self, user: UserProfile) -> UserProfile:
        self._mock_users[user.phone_number] = user
        if self._db:
            try:
                self.table.put_item(Item=self._user_item(user))
            except Exception as e:
                print(f"[DynamoDB] Error saving user: {e}")
        return user

    # ================= DOCTOR OPERATIONS =================
    def register_doctor(self, doctor: DoctorProfile) -> DoctorProfile:
        """Saves the doctor and the phone pointer used by `get_doctor_by_phone`. Two doctors with
        one phone: the last one saved owns the pointer."""
        self._mock_doctors[doctor.doctor_id] = doctor
        pointer = self._doctor_phone_item(doctor)
        if pointer:
            self._mock_doctor_phones[phone_key(doctor.phone_number)] = doctor.doctor_id
        if self._db:
            try:
                self.table.put_item(Item=self._doctor_item(doctor))
                if pointer:
                    self.table.put_item(Item=pointer)
            except Exception as e:
                print(f"[DynamoDB] Error saving doctor: {e}")
        return doctor

    def get_doctor_by_phone(self, phone: str) -> Optional[DoctorProfile]:
        """The doctor registered with this WhatsApp number, or None (WA-0).

        Reads the `DOCTORPHONE#<e164>` pointer and checks that the doctor still has that number (a
        stale pointer never matches). Doctors saved before the pointer existed are found with a
        scan, and the pointer is written then.
        """
        key = phone_key(phone)
        if not key:
            return None
        if self._db:
            try:
                item = self.table.get_item(Key={"PK": f"DOCTORPHONE#{key}", "SK": "PROFILE"}).get("Item")
                doctor = self.get_doctor(item["doctor_id"]) if item else None
                if doctor and phone_key(doctor.phone_number) == key:
                    return doctor
                # Every inbound message asks (DOC-1), and most senders are patients: the scan for
                # doctors without a pointer runs at most once a minute per process.
                now = datetime.now(timezone.utc).timestamp()
                scanned_at, by_phone = getattr(self, "_doctor_phone_scan", (0.0, {}))
                if now - scanned_at > DOCTOR_PHONE_SCAN_SECONDS:
                    by_phone = {phone_key(d.phone_number): d.doctor_id for d in self.list_doctors()}
                    self._doctor_phone_scan = (now, by_phone)
                doctor = self.get_doctor(by_phone[key]) if key in by_phone else None
                if doctor and phone_key(doctor.phone_number) == key:
                    self.table.put_item(Item=self._doctor_phone_item(doctor))
                    return doctor
                return None
            except Exception as e:
                print(f"[DynamoDB] Error looking up doctor by phone (using the in-memory mirror): {e}")
        doctor = self._mock_doctors.get(self._mock_doctor_phones.get(key, ""))
        if doctor and phone_key(doctor.phone_number) == key:
            return doctor
        return next((d for d in self._mock_doctors.values() if phone_key(d.phone_number) == key), None)

    def list_doctors(self) -> List[DoctorProfile]:
        if self._db:
            try:
                items = self._scan_all(FilterExpression=Attr("SK").eq("PROFILE") & Attr("PK").begins_with("DOCTOR#"))
                return [DoctorProfile(**_from_item(item["data"])) for item in items]
            except Exception as e:
                print(f"[DynamoDB] Error scanning doctors (using the in-memory mirror): {e}")
        return list(self._mock_doctors.values())

    def get_available_doctors_by_specialty(self, specialty: str) -> List[DoctorProfile]:
        """On-duty doctors of one specialty. DynamoDB mode: the GSI result is authoritative, so an
        empty result means nobody is on duty (never a stale in-memory doctor)."""
        if self._db:
            try:
                items = self._query_all(
                    IndexName="GSI_SpecialtyActive",
                    KeyConditionExpression=Key("GSI1PK").eq(f"SPECIALTY#{specialty}") & Key("GSI1SK").eq("STATUS#ACTIVE"),
                )
                return [DoctorProfile(**_from_item(item["data"])) for item in items]
            except Exception as e:
                print(f"[DynamoDB] Error querying available doctors by specialty (using the in-memory mirror): {e}")
        return [
            doc for doc in self._mock_doctors.values()
            if doc.specialty.lower() == specialty.lower() and doc.is_active
        ]

    def get_doctor(self, doctor_id: str) -> Optional[DoctorProfile]:
        """Reads a doctor from DynamoDB when connected, else from the in-memory store."""
        if self._db:
            try:
                item = self.table.get_item(Key={"PK": f"DOCTOR#{doctor_id}", "SK": "PROFILE"}).get("Item")
                return DoctorProfile(**_from_item(item["data"])) if item else None
            except Exception as e:
                print(f"[DynamoDB] Error fetching doctor {doctor_id} (using the in-memory mirror): {e}")
        return self._mock_doctors.get(doctor_id)

    def open_case_loads(self, exclude_case_id: Optional[str] = None) -> Dict[str, int]:
        """Load per doctor (AGENT-6, OFFER-1), from one read of the open (ACTIVE / DOCTOR_HANDOVER)
        cases: cases assigned to them plus cases currently offered to them (a doctor who declined is
        free again), so one doctor is not offered several cases at once. `exclude_case_id`: leave that
        case out (it is being offered again). Doctors without load are absent."""
        loads: Dict[str, int] = {}
        for c in self.list_active_cases():
            if c.case_id == exclude_case_id:
                continue
            ids = {c.assigned_doctor_id} if c.assigned_doctor_id else set()
            if c.doctor_offer_status == "PENDING":
                ids |= set(c.offered_doctor_ids or []) - set(c.declined_doctor_ids or [])
            for doctor_id in ids:
                loads[doctor_id] = loads.get(doctor_id, 0) + 1
        return loads

    # ---------------------------------------------------------------- doctor lock (OFFER-4)
    def _set_doctor_fields(self, doctor_id: str, fields: Dict[str, Any], condition: str = "",
                           names: Optional[Dict[str, str]] = None, values: Optional[Dict[str, Any]] = None,
                           matches: Optional[Callable[[DoctorProfile], bool]] = None) -> Optional[DoctorProfile]:
        """SETs data.<field> for `fields` only (an UpdateItem, like update_doctor_status), when the
        optional DynamoDB `condition` (with `names` / `values`) holds; in memory, when `matches(doctor)`
        does. Returns the updated doctor, or None if the doctor is missing or the condition failed."""
        if self._db:
            try:
                attr_names = {"#d": "data", **{f"#f{i}": k for i, k in enumerate(fields)}, **(names or {})}
                attr_values = {**{f":v{i}": _to_value(v) for i, v in enumerate(fields.values())}, **(values or {})}
                res = self.table.update_item(
                    Key={"PK": f"DOCTOR#{doctor_id}", "SK": "PROFILE"},
                    UpdateExpression="SET " + ", ".join(f"#d.#f{i} = :v{i}" for i in range(len(fields))),
                    ConditionExpression="attribute_exists(PK)" + (f" AND ({condition})" if condition else ""),
                    ExpressionAttributeNames=attr_names,
                    ExpressionAttributeValues=attr_values,
                    ReturnValues="ALL_NEW",
                )
                doc = DoctorProfile(**_from_item(res["Attributes"]["data"]))
                self._mock_doctors[doctor_id] = doc
                return doc
            except Exception as e:
                if _is_conditional_failure(e):
                    return None
                print(f"[DynamoDB] Error updating doctor {doctor_id} (using the in-memory mirror): {e}")
        with self._doctor_lock:
            doc = self._mock_doctors.get(doctor_id)
            if doc is None or (matches is not None and not matches(doc)):
                return None
            doc = doc.model_copy(update=fields)
            self._mock_doctors[doctor_id] = doc
            return doc

    def claim_doctor(self, doctor_id: str, case_id: str, kind: str, until: Optional[str] = None,
                     replace_case_id: Optional[str] = None) -> Optional[DoctorProfile]:
        """Locks the doctor to `case_id` (OFFER-4): `kind` OFFER (a pending offer, until `until`) or
        CASE (they hold it). Atomic: succeeds only if the doctor is free, already locked to this case,
        holds an OFFER lock that has expired, or is locked to `replace_case_id` (the caller saw that
        lock's case is closed or no longer theirs). None: the doctor is busy (or missing)."""
        now = datetime.now(timezone.utc).isoformat()
        fields = {"busy_case_id": case_id, "busy_kind": kind, "busy_until": until}
        condition = ("attribute_not_exists(#d.#b) OR #d.#b = :null OR #d.#b = :case "
                     "OR (#d.#k = :offer AND #d.#u < :now)")
        values: Dict[str, Any] = {":null": None, ":case": case_id, ":offer": "OFFER", ":now": now}
        if replace_case_id:
            condition += " OR #d.#b = :stale"
            values[":stale"] = replace_case_id

        def free(doc: DoctorProfile) -> bool:
            return (not doc.busy_case_id or doc.busy_case_id in (case_id, replace_case_id)
                    or (doc.busy_kind == "OFFER" and (doc.busy_until or "") < now))
        return self._set_doctor_fields(doctor_id, fields, condition,
                                       names={"#b": "busy_case_id", "#k": "busy_kind", "#u": "busy_until"},
                                       values=values, matches=free)

    def release_doctor(self, doctor_id: str, case_id: str, closed: bool = False) -> Optional[DoctorProfile]:
        """Frees the doctor if they are locked to `case_id` (a lock for another case is left alone).
        `closed`: they held the case until it closed; remembered for a late message (DOC-1).
        None if they were not locked to it."""
        fields: Dict[str, Any] = {"busy_case_id": None, "busy_kind": None, "busy_until": None}
        if closed:
            fields.update(last_case_id=case_id, last_case_closed_at=datetime.now(timezone.utc).isoformat())
        return self._set_doctor_fields(doctor_id, fields, "#d.#b = :case", names={"#b": "busy_case_id"},
                                       values={":case": case_id}, matches=lambda d: d.busy_case_id == case_id)

    def update_doctor_fields(self, doctor_id: str, fields: Dict[str, Any]) -> Optional[DoctorProfile]:
        """Sets the given DoctorProfile fields only (e.g. last_inbound_at). None if the doctor is missing."""
        fields = {k: v for k, v in fields.items() if k in DoctorProfile.model_fields and k != "doctor_id"}
        return self._set_doctor_fields(doctor_id, fields) if fields else self.get_doctor(doctor_id)

    def update_doctor_profile(self, doctor_id: str, fields: Dict[str, Any]) -> Optional[DoctorProfile]:
        """An administrator's edit (name, phone, licence, specialty, bio, ...): the fields, plus the
        specialty index key and the phone pointer when those change. None if the doctor is missing."""
        before = self.get_doctor(doctor_id)
        if before is None:
            return None
        doc = self.update_doctor_fields(doctor_id, fields)
        if doc is None:
            return None
        if doc.specialty != before.specialty and self._db:
            try:
                self.table.update_item(Key={"PK": f"DOCTOR#{doctor_id}", "SK": "PROFILE"},
                                       UpdateExpression="SET GSI1PK = :s",
                                       ExpressionAttributeValues={":s": f"SPECIALTY#{doc.specialty}"})
            except Exception as e:
                print(f"[DynamoDB] Error moving doctor {doctor_id} to specialty {doc.specialty}: {e}")
        old_key, new_key = phone_key(before.phone_number), phone_key(doc.phone_number)
        if old_key != new_key:
            self._move_doctor_phone(doc, old_key)
        return doc

    def _move_doctor_phone(self, doctor: DoctorProfile, old_key: str) -> None:
        if self._mock_doctor_phones.get(old_key) == doctor.doctor_id:
            self._mock_doctor_phones.pop(old_key, None)
        pointer = self._doctor_phone_item(doctor)
        if pointer:
            self._mock_doctor_phones[phone_key(doctor.phone_number)] = doctor.doctor_id
        self._doctor_phone_scan = (0.0, {})  # the cached scan may point at the old number
        if self._db:
            try:
                if old_key:
                    self.table.delete_item(Key={"PK": f"DOCTORPHONE#{old_key}", "SK": "PROFILE"},
                                           ConditionExpression="doctor_id = :d",
                                           ExpressionAttributeValues={":d": doctor.doctor_id})
            except Exception as e:
                if not _is_conditional_failure(e):
                    print(f"[DynamoDB] Error removing the old phone pointer of {doctor.doctor_id}: {e}")
            try:
                if pointer:
                    self.table.put_item(Item=pointer)
            except Exception as e:
                print(f"[DynamoDB] Error saving the phone pointer of {doctor.doctor_id}: {e}")

    def delete_doctor(self, doctor_id: str) -> bool:
        """Unregisters a doctor: the profile and its phone pointer. Their past cases keep the name.
        True if the doctor existed."""
        doctor = self.get_doctor(doctor_id)
        if doctor is None:
            return False
        self._mock_doctors.pop(doctor_id, None)
        key = phone_key(doctor.phone_number)
        if self._mock_doctor_phones.get(key) == doctor_id:
            self._mock_doctor_phones.pop(key, None)
        self._doctor_phone_scan = (0.0, {})
        if self._db:
            try:
                self.table.delete_item(Key={"PK": f"DOCTOR#{doctor_id}", "SK": "PROFILE"})
                if key:
                    self.table.delete_item(Key={"PK": f"DOCTORPHONE#{key}", "SK": "PROFILE"},
                                           ConditionExpression="doctor_id = :d",
                                           ExpressionAttributeValues={":d": doctor_id})
            except Exception as e:
                if not _is_conditional_failure(e):
                    print(f"[DynamoDB] Error deleting doctor {doctor_id}: {e}")
        return True

    def list_waiting_cases(self) -> List[EmergencyCase]:
        """Open cases waiting for a doctor (OFFER-5: no free doctor took them), lowest ESI first,
        then the longest waiting."""
        waiting = [c for c in self.list_active_cases()
                   if c.doctor_offer_status == "EXHAUSTED" and not c.assigned_doctor_id]
        return sorted(waiting, key=lambda c: (c.esi_level or 5, c.no_doctor_since or c.created_at))

    def count_open_cases_for_doctor(self, doctor_id: str) -> int:
        """One doctor's load (see open_case_loads)."""
        return self.open_case_loads().get(doctor_id, 0)

    def increment_doctor_cases_handled(self, doctor_id: str) -> Optional[DoctorProfile]:
        """Atomically adds 1 to the doctor's lifetime cases_handled counter."""
        if self._db:
            try:
                res = self.table.update_item(
                    Key={"PK": f"DOCTOR#{doctor_id}", "SK": "PROFILE"},
                    UpdateExpression="ADD #d.#c :one",
                    ConditionExpression="attribute_exists(PK)",
                    ExpressionAttributeNames={"#d": "data", "#c": "cases_handled"},
                    ExpressionAttributeValues={":one": 1},
                    ReturnValues="ALL_NEW",
                )
                doc = DoctorProfile(**_from_item(res["Attributes"]["data"]))
                if doctor_id in self._mock_doctors:
                    self._mock_doctors[doctor_id] = doc  # keep the fallback mirror current, never add to it
                return doc
            except Exception as e:
                if _is_conditional_failure(e):
                    return None
                print(f"[DynamoDB] Error incrementing cases_handled for {doctor_id} (using the in-memory mirror): {e}")
        doc = self._mock_doctors.get(doctor_id)
        if not doc:
            return None
        doc = doc.model_copy(update={"cases_handled": doc.cases_handled + 1})
        self._mock_doctors[doctor_id] = doc
        return doc

    def update_doctor_status(self, doctor_id: str, is_active: bool) -> Optional[DoctorProfile]:
        """Sets the duty flag and the GSI status key only (an UpdateItem, so a concurrent
        cases_handled increment is never overwritten)."""
        if self._db:
            try:
                res = self.table.update_item(
                    Key={"PK": f"DOCTOR#{doctor_id}", "SK": "PROFILE"},
                    UpdateExpression="SET #d.#a = :a, GSI1SK = :s",
                    ConditionExpression="attribute_exists(PK)",
                    ExpressionAttributeNames={"#d": "data", "#a": "is_active"},
                    ExpressionAttributeValues={":a": is_active, ":s": f"STATUS#{'ACTIVE' if is_active else 'INACTIVE'}"},
                    ReturnValues="ALL_NEW",
                )
                doc = DoctorProfile(**_from_item(res["Attributes"]["data"]))
                self._mock_doctors[doctor_id] = doc
                return doc
            except Exception as e:
                if _is_conditional_failure(e):
                    return None
                print(f"[DynamoDB] Error updating doctor status (using the in-memory mirror): {e}")
        doc = self._mock_doctors.get(doctor_id)
        if doc:
            doc = doc.model_copy(update={"is_active": is_active})
            self._mock_doctors[doctor_id] = doc
        return doc

    def update_doctor_portal(self, doctor_id: str, portal_status: str, portal_invited_at: Optional[str],
                             email: Optional[str] = None) -> Optional[DoctorProfile]:
        """Sets the portal invitation fields (and the email, when given) only: an UpdateItem, like
        update_doctor_status."""
        fields = {"portal_status": portal_status, "portal_invited_at": portal_invited_at}
        if email:
            fields["email"] = email
        if self._db:
            try:
                res = self.table.update_item(
                    Key={"PK": f"DOCTOR#{doctor_id}", "SK": "PROFILE"},
                    UpdateExpression="SET " + ", ".join(f"#d.#f{i} = :v{i}" for i in range(len(fields))),
                    ConditionExpression="attribute_exists(PK)",
                    ExpressionAttributeNames={"#d": "data", **{f"#f{i}": k for i, k in enumerate(fields)}},
                    ExpressionAttributeValues={f":v{i}": v for i, v in enumerate(fields.values())},
                    ReturnValues="ALL_NEW",
                )
                doc = DoctorProfile(**_from_item(res["Attributes"]["data"]))
                self._mock_doctors[doctor_id] = doc
                return doc
            except Exception as e:
                if _is_conditional_failure(e):
                    return None
                print(f"[DynamoDB] Error updating doctor portal status (using the in-memory mirror): {e}")
        doc = self._mock_doctors.get(doctor_id)
        if doc:
            doc = doc.model_copy(update=fields)
            self._mock_doctors[doctor_id] = doc
        return doc

    # ================= ADMINISTRATOR OPERATIONS (ADMIN-2) =================
    @staticmethod
    def _admin_item(admin: AdminProfile) -> Dict[str, Any]:
        return {"PK": f"ADMIN#{admin.admin_id}", "SK": "PROFILE", "data": _to_item(admin)}

    def save_admin(self, admin: AdminProfile) -> AdminProfile:
        """Saves (creates or replaces) an administrator added in the portal."""
        self._mock_admins[admin.admin_id] = admin
        if self._db:
            try:
                self.table.put_item(Item=self._admin_item(admin))
            except Exception as e:
                print(f"[DynamoDB] Error saving administrator: {e}")
        return admin

    def list_admins(self) -> List[AdminProfile]:
        """Administrators added in the portal, oldest first."""
        admins = None
        if self._db:
            try:
                items = self._scan_all(FilterExpression=Attr("SK").eq("PROFILE") & Attr("PK").begins_with("ADMIN#"))
                admins = [AdminProfile(**_from_item(item["data"])) for item in items]
            except Exception as e:
                print(f"[DynamoDB] Error scanning administrators (using the in-memory mirror): {e}")
        if admins is None:
            admins = list(self._mock_admins.values())
        return sorted(admins, key=lambda a: a.registered_at)

    def get_admin(self, admin_id: str) -> Optional[AdminProfile]:
        if self._db:
            try:
                item = self.table.get_item(Key={"PK": f"ADMIN#{admin_id}", "SK": "PROFILE"}).get("Item")
                return AdminProfile(**_from_item(item["data"])) if item else None
            except Exception as e:
                print(f"[DynamoDB] Error fetching administrator {admin_id} (using the in-memory mirror): {e}")
        return self._mock_admins.get(admin_id)

    def update_admin_fields(self, admin_id: str, fields: Dict[str, Any]) -> Optional[AdminProfile]:
        """Sets the given AdminProfile fields only (an UpdateItem). None if the administrator is missing."""
        fields = {k: v for k, v in fields.items() if k in AdminProfile.model_fields and k != "admin_id"}
        if not fields:
            return self.get_admin(admin_id)
        if self._db:
            try:
                res = self.table.update_item(
                    Key={"PK": f"ADMIN#{admin_id}", "SK": "PROFILE"},
                    UpdateExpression="SET " + ", ".join(f"#d.#f{i} = :v{i}" for i in range(len(fields))),
                    ConditionExpression="attribute_exists(PK)",
                    ExpressionAttributeNames={"#d": "data", **{f"#f{i}": k for i, k in enumerate(fields)}},
                    ExpressionAttributeValues={f":v{i}": v for i, v in enumerate(fields.values())},
                    ReturnValues="ALL_NEW",
                )
                admin = AdminProfile(**_from_item(res["Attributes"]["data"]))
                self._mock_admins[admin_id] = admin
                return admin
            except Exception as e:
                if _is_conditional_failure(e):
                    return None
                print(f"[DynamoDB] Error updating administrator {admin_id} (using the in-memory mirror): {e}")
        admin = self._mock_admins.get(admin_id)
        if admin:
            admin = admin.model_copy(update=fields)
            self._mock_admins[admin_id] = admin
        return admin

    def delete_admin(self, admin_id: str) -> bool:
        """Removes an administrator record. True if it existed."""
        existed = self._mock_admins.pop(admin_id, None) is not None
        if self._db:
            try:
                res = self.table.delete_item(Key={"PK": f"ADMIN#{admin_id}", "SK": "PROFILE"}, ReturnValues="ALL_OLD")
                return "Attributes" in res
            except Exception as e:
                print(f"[DynamoDB] Error deleting administrator {admin_id}: {e}")
        return existed

    # ================= CASE OPERATIONS =================
    def save_case(self, case: EmergencyCase) -> EmergencyCase:
        # Metric counters (DATA-3) move only when the case is first created, never on a re-save.
        is_new_in_memory = case.case_id not in self._mock_cases
        self._mock_cases[case.case_id] = case
        if self._db:
            try:
                res = self.table.put_item(
                    Item={
                        "PK": f"CASE#{case.case_id}",
                        "SK": "METADATA",
                        "GSI1PK": f"STATUS#{case.status}",
                        "GSI1SK": f"DATE#{case.created_at[:10]}",
                        "GSI2PK": f"MONTH#{case.created_at[:7]}",
                        "GSI2SK": case.created_at,
                        "data": _to_item(case)
                    },
                    # The old item comes back only if the case already existed: atomic first-create test.
                    ReturnValues="ALL_OLD",
                )
                if not res.get("Attributes"):
                    self._increment_case_metrics(case)
            except Exception as e:
                print(f"[DynamoDB] Error saving case: {e}")
        elif is_new_in_memory:
            self._increment_case_metrics_in_memory(case)
        return case

    def get_case(self, case_id: str) -> Optional[EmergencyCase]:
        if self._db:
            try:
                item = self.table.get_item(Key={"PK": f"CASE#{case_id}", "SK": "METADATA"}).get("Item")
                return EmergencyCase(**_from_item(item["data"])) if item else None
            except Exception as e:
                print(f"[DynamoDB] Error fetching case {case_id} (using the in-memory mirror): {e}")
        return self._mock_cases.get(case_id)

    def delete_case(self, case_id: str) -> Optional[Tuple[EmergencyCase, List[ChatMessage]]]:
        """Deletes a case and its chat, and takes it out of its month's counters (analytics then
        match the cases that remain). Returns what was deleted (for the media in S3), or None."""
        case = self.get_case(case_id)
        if case is None:
            return None
        messages = self.get_case_messages(case_id)
        self._mock_cases.pop(case_id, None)
        self._mock_messages.pop(case_id, None)
        deltas = {k: -v for k, v in self._case_metric_deltas(case).items()}
        if self._db:
            try:
                keys = [{"PK": f"CASE#{case_id}", "SK": "METADATA"}]
                keys += [{"PK": f"CASE#{case_id}", "SK": item["SK"]} for item in self._query_all(
                    KeyConditionExpression=Key("PK").eq(f"CASE#{case_id}") & Key("SK").begins_with("MSG#"),
                    ProjectionExpression="SK")]
                with self.table.batch_writer() as batch:
                    for key in keys:
                        batch.delete_item(Key=key)
                self._add_metric_deltas(case.created_at[:7], deltas, case_id)
            except Exception as e:
                print(f"[DynamoDB] Error deleting case {case_id}: {e}")
                raise
        else:
            counters = self._mock_metrics.setdefault(case.created_at[:7], {})
            for name, delta in deltas.items():
                counters[name] = counters.get(name, 0) + delta
        return case, messages

    def list_all_cases(self) -> List[EmergencyCase]:
        if self._db:
            try:
                items = self._scan_all(FilterExpression=Attr("SK").eq("METADATA") & Attr("PK").begins_with("CASE#"))
                cases = [EmergencyCase(**_from_item(item["data"])) for item in items]
                return sorted(cases, key=lambda x: x.created_at, reverse=True)
            except Exception as e:
                print(f"[DynamoDB] Error scanning all cases (using the in-memory mirror): {e}")
        return sorted(list(self._mock_cases.values()), key=lambda x: x.created_at, reverse=True)

    def list_active_cases(self) -> List[EmergencyCase]:
        """Open (ACTIVE / DOCTOR_HANDOVER) cases, newest first. DynamoDB: paginated GSI_SpecialtyActive
        queries on GSI1PK=STATUS#<status> (written by save_case / update_case), no table scan."""
        if self._db:
            try:
                items: List[Dict[str, Any]] = []
                for status in OPEN_CASE_STATUSES:
                    items.extend(self._query_all(IndexName="GSI_SpecialtyActive",
                                                 KeyConditionExpression=Key("GSI1PK").eq(f"STATUS#{status}")))
                cases = [EmergencyCase(**_from_item(item["data"])) for item in items]
                return sorted([c for c in cases if c.status in OPEN_CASE_STATUSES],
                              key=lambda x: x.created_at, reverse=True)
            except Exception as e:
                print(f"[DynamoDB] Error querying open cases (using the in-memory mirror): {e}")
        return sorted([c for c in self._mock_cases.values() if c.status in OPEN_CASE_STATUSES],
                      key=lambda x: x.created_at, reverse=True)

    def get_open_case_for_user(self, phone_number: str) -> Optional[EmergencyCase]:
        """The user's open (ACTIVE / DOCTOR_HANDOVER) case, via UserProfile.active_case_id."""
        user = self.get_user(phone_number)
        if not user or not user.active_case_id:
            return None
        case = self.get_case(user.active_case_id)
        if case and case.status in OPEN_CASE_STATUSES:
            return case
        return None

    def update_case(self, case_id: str, update_dict: Dict[str, Any]) -> Optional[EmergencyCase]:
        """Changes only the given fields. DynamoDB: one UpdateItem that SETs data.<field> per key
        (plus the GSI keys a status / created_at change moves), conditional on the case existing,
        so concurrent writers to different fields (API and worker) never clobber each other.
        Unknown keys and case_id are ignored, as the old model rebuild did. None if the case is missing."""
        changes = {k: _case_field_adapter(k).validate_python(v)
                   for k, v in (update_dict or {}).items() if k in EmergencyCase.model_fields and k != "case_id"}
        before = self.get_case(case_id) if _METRIC_FIELDS & changes.keys() else None
        updated_case = self._apply_case_update(case_id, changes)
        if before and updated_case:
            self._rebalance_case_metrics(before, updated_case)
        return updated_case

    def update_case_if(
        self,
        case_id: str,
        update_dict: Dict[str, Any],
        expect: Optional[Dict[str, Any]] = None,
        member: Optional[Tuple[str, Any]] = None,
        not_member: Optional[Tuple[str, Any]] = None,
        append: Optional[Dict[str, List[Any]]] = None,
    ) -> Optional[EmergencyCase]:
        """Conditional update_case (OFFER-1): applies the changes only if, atomically,
        - every `expect` field equals its value (None: missing or null),
        - `member` (field, value): the list field contains the value,
        - `not_member` (field, value): the list field does not contain it;
        and appends `append` items to list fields. Returns the updated case, or None if the
        condition failed or the case is missing. DynamoDB: one conditional UpdateItem; in memory:
        the same check under a lock."""
        changes = {k: _case_field_adapter(k).validate_python(v)
                   for k, v in (update_dict or {}).items() if k in EmergencyCase.model_fields and k != "case_id"}
        append = {k: list(v) for k, v in (append or {}).items() if k in EmergencyCase.model_fields}
        before = self.get_case(case_id) if _METRIC_FIELDS & changes.keys() else None
        updated = None
        done = False
        if self._db:
            try:
                updated = self._update_case_dynamo(case_id, changes, expect=expect, member=member,
                                                   not_member=not_member, append=append)
                done = True
            except Exception as e:
                if _is_conditional_failure(e):
                    return None
                print(f"[DynamoDB] Error in conditional update of case {case_id} (using the in-memory mirror): {e}")
        if not done:
            with self._case_lock:
                case = self._mock_cases.get(case_id)
                if not case or not self._case_matches(case, expect, member, not_member):
                    return None
                data = case.model_dump()
                data.update(changes)
                for field_name, items in append.items():
                    data[field_name] = list(data.get(field_name) or []) + items
                updated = EmergencyCase(**data)
                self._mock_cases[case_id] = updated
        if before and updated:
            self._rebalance_case_metrics(before, updated)
        return updated

    @staticmethod
    def _case_matches(case: EmergencyCase, expect: Optional[Dict[str, Any]],
                      member: Optional[Tuple[str, Any]], not_member: Optional[Tuple[str, Any]]) -> bool:
        if any(getattr(case, k, None) != v for k, v in (expect or {}).items()):
            return False
        if member and member[1] not in (getattr(case, member[0], None) or []):
            return False
        if not_member and not_member[1] in (getattr(case, not_member[0], None) or []):
            return False
        return True

    def _apply_case_update(self, case_id: str, changes: Dict[str, Any]) -> Optional[EmergencyCase]:
        if self._db:
            try:
                return self._update_case_dynamo(case_id, changes)
            except Exception as e:
                if _is_conditional_failure(e):
                    return None
                print(f"[DynamoDB] Error updating case {case_id} (using the in-memory mirror): {e}")
        with self._case_lock:
            case = self._mock_cases.get(case_id)
            if not case:
                return None
            case_data = case.model_dump()
            case_data.update(changes)
            updated_case = EmergencyCase(**case_data)
            self._mock_cases[case_id] = updated_case
            return updated_case

    def _update_case_dynamo(
        self,
        case_id: str,
        changes: Dict[str, Any],
        expect: Optional[Dict[str, Any]] = None,
        member: Optional[Tuple[str, Any]] = None,
        not_member: Optional[Tuple[str, Any]] = None,
        append: Optional[Dict[str, List[Any]]] = None,
    ) -> Optional[EmergencyCase]:
        key = {"PK": f"CASE#{case_id}", "SK": "METADATA"}
        conditional = bool(expect or member or not_member)
        if not changes and not append and not conditional:
            item = self.table.get_item(Key=key).get("Item")
            return EmergencyCase(**_from_item(item["data"])) if item else None
        names: Dict[str, str] = {"#d": "data"}
        values: Dict[str, Any] = {}
        sets: List[str] = []
        conditions: List[str] = ["attribute_exists(PK)"]
        for i, (field_name, value) in enumerate(changes.items()):
            names[f"#f{i}"] = field_name
            values[f":v{i}"] = _to_value(value)
            sets.append(f"#d.#f{i} = :v{i}")
        for i, (field_name, items) in enumerate((append or {}).items()):
            names[f"#a{i}"] = field_name
            values[f":a{i}"] = _to_value(items)
            values[":empty_list"] = []
            sets.append(f"#d.#a{i} = list_append(if_not_exists(#d.#a{i}, :empty_list), :a{i})")
        for i, (field_name, value) in enumerate((expect or {}).items()):
            names[f"#e{i}"] = field_name
            if value is None:
                values[":null"] = None
                conditions.append(f"(attribute_not_exists(#d.#e{i}) OR #d.#e{i} = :null)")
            else:
                values[f":e{i}"] = _to_value(value)
                conditions.append(f"#d.#e{i} = :e{i}")
        if member:
            names["#m"], values[":m"] = member[0], _to_value(member[1])
            conditions.append("contains(#d.#m, :m)")
        if not_member:
            names["#n"], values[":n"] = not_member[0], _to_value(not_member[1])
            conditions.append("NOT contains(#d.#n, :n)")
        if not sets:
            # A condition-only call still needs an update expression: rewrite case_id with itself.
            names["#noop"] = "case_id"
            values[":noop"] = case_id
            sets.append("#d.#noop = :noop")
        if "status" in changes:
            values[":gsi1pk"] = f"STATUS#{changes['status']}"
            sets.append("GSI1PK = :gsi1pk")
        if "created_at" in changes:
            created = str(changes["created_at"])
            values.update({":gsi1sk": f"DATE#{created[:10]}", ":gsi2pk": f"MONTH#{created[:7]}", ":gsi2sk": created})
            sets += ["GSI1SK = :gsi1sk", "GSI2PK = :gsi2pk", "GSI2SK = :gsi2sk"]
        res = self.table.update_item(
            Key=key,
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression=" AND ".join(conditions),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
        updated = EmergencyCase(**_from_item(res["Attributes"]["data"]))
        self._mock_cases[case_id] = updated  # fallback mirror
        return updated

    # ================= CHAT MESSAGE OPERATIONS =================
    def save_chat_message(self, message: ChatMessage) -> ChatMessage:
        msgs = self._mock_messages.setdefault(message.case_id, [])
        msgs[:] = [m for m in msgs if m.message_id != message.message_id]
        msgs.append(message)
        if self._db:
            try:
                self.table.put_item(
                    Item={
                        "PK": f"CASE#{message.case_id}",
                        "SK": f"MSG#{message.timestamp}#{message.message_id}",
                        "data": _to_item(message)
                    }
                )
            except Exception as e:
                print(f"[DynamoDB] Error saving chat message: {e}")
        return message

    def get_case_messages(self, case_id: str) -> List[ChatMessage]:
        if self._db:
            try:
                items = self._query_all(KeyConditionExpression=Key("PK").eq(f"CASE#{case_id}") & Key("SK").begins_with("MSG#"))
                return [ChatMessage(**_from_item(item["data"])) for item in items]
            except Exception as e:
                print(f"[DynamoDB] Error querying case messages (using the in-memory mirror): {e}")
        return self._mock_messages.get(case_id, [])

    # ================= IDEMPOTENCY =================
    def claim_idempotency_key(self, key: str, lease_seconds: int = 300) -> bool:
        """Claims `key` for processing. Returns False if it is already COMPLETE, or IN_PROGRESS
        with an unexpired lease (another worker is on it). An expired IN_PROGRESS claim (a worker
        died mid-message) can be re-claimed, so SQS retries are not lost."""
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(seconds=lease_seconds)).isoformat()
        if self._db:
            try:
                self.table.put_item(
                    Item={"PK": f"IDEMPOTENCY#{key}", "SK": "LOCK", "status": "IN_PROGRESS",
                          "claimed_at": now.isoformat(),
                          "ttl": int((now + timedelta(days=7)).timestamp())},
                    ConditionExpression="attribute_not_exists(PK) OR (#s = :inprog AND claimed_at < :cutoff)",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":inprog": "IN_PROGRESS", ":cutoff": cutoff},
                )
                return True
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    return False
                print(f"[DynamoDB] Error claiming idempotency key {key}: {e}")
            except Exception as e:
                print(f"[DynamoDB] Error claiming idempotency key {key}: {e}")
        rec = self._mock_idempotency.get(key)
        if rec and (rec["status"] == "COMPLETE" or rec["claimed_at"] >= cutoff):
            return False
        self._mock_idempotency[key] = {"status": "IN_PROGRESS", "claimed_at": now.isoformat(), "result": None}
        return True

    def complete_idempotency_key(self, key: str, result: Optional[Dict[str, Any]] = None) -> None:
        """Marks `key` COMPLETE and stores a small JSON-able result (e.g. {"case_id": ...})."""
        result = result or {}
        rec = self._mock_idempotency.setdefault(key, {"claimed_at": datetime.now(timezone.utc).isoformat()})
        rec.update({"status": "COMPLETE", "result": result})
        if self._db:
            try:
                self.table.update_item(
                    Key={"PK": f"IDEMPOTENCY#{key}", "SK": "LOCK"},
                    UpdateExpression="SET #s = :c, #r = :r",
                    ExpressionAttributeNames={"#s": "status", "#r": "result"},
                    ExpressionAttributeValues={":c": "COMPLETE", ":r": json.loads(json.dumps(result), parse_float=Decimal)},
                )
            except Exception as e:
                print(f"[DynamoDB] Error completing idempotency key {key}: {e}")

    def release_idempotency_key(self, key: str) -> None:
        """Drops an IN_PROGRESS claim after a handled failure so the retry can claim it at once."""
        self._mock_idempotency.pop(key, None)
        if self._db:
            try:
                self.table.delete_item(Key={"PK": f"IDEMPOTENCY#{key}", "SK": "LOCK"})
            except Exception as e:
                print(f"[DynamoDB] Error releasing idempotency key {key}: {e}")

    def get_idempotency_record(self, key: str) -> Optional[Dict[str, Any]]:
        """{"status": "IN_PROGRESS"|"COMPLETE", "claimed_at": ..., "result": {...}} or None."""
        if self._db:
            try:
                item = self.table.get_item(Key={"PK": f"IDEMPOTENCY#{key}", "SK": "LOCK"}).get("Item")
                if not item:
                    return None
                return {"status": item.get("status"), "claimed_at": item.get("claimed_at"),
                        "result": _from_item(item.get("result") or {})}
            except Exception as e:
                print(f"[DynamoDB] Error reading idempotency key {key}: {e}")
        return self._mock_idempotency.get(key)

    # ================= DIRECT-CALL LINKS (CALL-3 fallback) =================
    def save_direct_call_link(self, token: str, link: Dict[str, Any], expires_at: datetime) -> None:
        """Stores a direct-call link (case_id, doctor_id, ...) until `expires_at` (DynamoDB TTL)."""
        record = {**link, "expires_at": expires_at.isoformat()}
        if self._db:
            self.table.put_item(Item={"PK": f"DIRECTCALL#{token}", "SK": "LINK", **record,
                                      "ttl": int(expires_at.timestamp())})
            return
        self._mock_direct_call_links[token] = record

    def get_direct_call_link(self, token: str) -> Optional[Dict[str, Any]]:
        """The link's record, or None if unknown. Expiry is checked by the caller (TTL deletes late)."""
        if self._db:
            try:
                item = self.table.get_item(Key={"PK": f"DIRECTCALL#{token}", "SK": "LINK"}).get("Item")
                if not item:
                    return None
                return {k: _from_item(v) for k, v in item.items() if k not in ("PK", "SK", "ttl")}
            except Exception as e:
                print(f"[DynamoDB] Error reading direct-call link: {e}")
                return None
        return self._mock_direct_call_links.get(token)

    # ================= METRICS (DATA-3) =================
    # One record per month: PK=METRIC#<YYYY-MM>, SK=SUMMARY, flat numeric counters updated with an
    # atomic ADD when a case is first created. Counter names: cases_total, esi_1_2, esi_3, esi_4_5,
    # lang#<language>, spec#<specialty> (ESI 1-3 only), doc#<doctor name>.

    @staticmethod
    def _case_metric_deltas(case: EmergencyCase) -> Dict[str, int]:
        deltas: Dict[str, int] = {"cases_total": 1}
        if case.esi_level in (1, 2):
            deltas["esi_1_2"] = 1
        elif case.esi_level == 3:
            deltas["esi_3"] = 1
        else:
            deltas["esi_4_5"] = 1
        deltas[f"lang#{case.detected_language or 'Unknown'}"] = 1
        if case.esi_level in (1, 2, 3):
            deltas[f"spec#{case.matched_specialty or 'General Emergency'}"] = 1
        if case.assigned_doctor_name:
            deltas[f"doc#{case.assigned_doctor_name}"] = 1
        return deltas

    def _rebalance_case_metrics(self, before: EmergencyCase, after: EmergencyCase) -> None:
        """Moves an existing case's counters when its ESI bucket, specialty or doctor changes
        (e.g. a human-review or ESI 3 case escalated to ESI 1). cases_total is unchanged."""
        old, new = self._case_metric_deltas(before), self._case_metric_deltas(after)
        deltas = {k: new.get(k, 0) - old.get(k, 0) for k in old.keys() | new.keys()}
        deltas = {k: v for k, v in deltas.items() if v}
        if not deltas:
            return
        if self._db:
            self._add_metric_deltas(before.created_at[:7], deltas, before.case_id)
        else:
            counters = self._mock_metrics.setdefault(before.created_at[:7], {})
            for name, delta in deltas.items():
                counters[name] = counters.get(name, 0) + delta

    def _increment_case_metrics(self, case: EmergencyCase) -> None:
        """Atomic ADD of the case's counters into its month's METRIC record (DynamoDB)."""
        self._add_metric_deltas(case.created_at[:7], self._case_metric_deltas(case), case.case_id)

    def _add_metric_deltas(self, month: str, deltas: Dict[str, int], case_id: str) -> None:
        names = {f"#m{i}": name for i, name in enumerate(deltas)}
        values = {f":v{i}": delta for i, delta in enumerate(deltas.values())}
        expr = "ADD " + ", ".join(f"#m{i} :v{i}" for i in range(len(deltas)))
        try:
            self.table.update_item(
                Key={"PK": f"METRIC#{month}", "SK": "SUMMARY"},
                UpdateExpression=expr,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except Exception as e:
            print(f"[DynamoDB] Error updating metrics for case {case_id}: {e}")

    def _increment_case_metrics_in_memory(self, case: EmergencyCase) -> None:
        month = case.created_at[:7]
        counters = self._mock_metrics.setdefault(month, {})
        for name, delta in self._case_metric_deltas(case).items():
            counters[name] = counters.get(name, 0) + delta

    @property
    def _mock_metrics(self) -> Dict[str, Dict[str, int]]:
        # Created lazily so __init__ (frozen) needs no change.
        if not hasattr(self, "_mock_metrics_store"):
            self._mock_metrics_store: Dict[str, Dict[str, int]] = {}
        return self._mock_metrics_store

    def get_month_metrics(self, month: str) -> Optional[Dict[str, int]]:
        """Counters of the METRIC#<month> summary record, or None if the month has none."""
        if self._db:
            try:
                item = self.table.get_item(Key={"PK": f"METRIC#{month}", "SK": "SUMMARY"}).get("Item")
                if not item:
                    return None
                return {k: int(v) for k, v in item.items() if k not in ("PK", "SK")}
            except Exception as e:
                print(f"[DynamoDB] Error reading metrics for {month}: {e}")
        counters = self._mock_metrics.get(month)
        return dict(counters) if counters is not None else None

    def list_cases_for_month(self, month: str) -> List[EmergencyCase]:
        """Cases created in <YYYY-MM>, via a GSI_Analytics query (no table scan)."""
        if self._db:
            try:
                items = self._query_all(IndexName="GSI_Analytics", KeyConditionExpression=Key("GSI2PK").eq(f"MONTH#{month}"))
                return [EmergencyCase(**_from_item(item["data"])) for item in items]
            except Exception as e:
                print(f"[DynamoDB] Error querying cases for {month}: {e}")
        return [c for c in self._mock_cases.values() if c.created_at[:7] == month]

    def _count_users(self) -> int:
        """Registered users. DynamoDB: paginated COUNT scan of USER# profiles (there is no user
        counter because save_user is outside the metrics hook); in-memory: the user dict."""
        if self._db:
            try:
                kwargs = {"FilterExpression": Attr("SK").eq("PROFILE") & Attr("PK").begins_with("USER#"),
                          "Select": "COUNT"}
                total = 0
                while True:
                    res = self.table.scan(**kwargs)
                    total += res.get("Count", 0)
                    if "LastEvaluatedKey" not in res:
                        return total
                    kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]
            except Exception as e:
                print(f"[DynamoDB] Error counting users: {e}")
        return len(self._mock_users)

    # ================= ANALYTICS =================
    @staticmethod
    def _avg_response_seconds(cases: List[EmergencyCase]) -> Optional[float]:
        """Mean of dispatched_at - received_at over cases that have both (DATA-1)."""
        return DynamoDBService._avg_seconds(cases, "received_at", "dispatched_at")

    @staticmethod
    def _avg_seconds(cases: List[EmergencyCase], start_field: str, end_field: str) -> Optional[float]:
        """Mean of end - start over cases that have both timestamps; None if none has."""
        durations = []
        for c in cases:
            start, end = getattr(c, start_field, None), getattr(c, end_field, None)
            if not (start and end):
                continue
            try:
                started = datetime.fromisoformat(start.replace("Z", "+00:00"))
                ended = datetime.fromisoformat(end.replace("Z", "+00:00"))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                if ended.tzinfo is None:
                    ended = ended.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            seconds = (ended - started).total_seconds()
            if seconds >= 0:
                durations.append(seconds)
        return round(sum(durations) / len(durations), 2) if durations else None

    def get_analytics_summary(self, month: Optional[str] = None) -> AnalyticsSummary:
        """KPIs for one month (default: the current UTC month), from the METRIC#<month> counters plus
        a GSI_Analytics query for response times. Real data only unless settings.DEMO_MODE."""
        month = month or datetime.now(timezone.utc).strftime("%Y-%m")
        cases = self.list_cases_for_month(month)
        counters = self.get_month_metrics(month)
        if counters is None:
            # Cases written before metric records existed: rebuild the counters from the month's cases.
            counters = {}
            for c in cases:
                for name, delta in self._case_metric_deltas(c).items():
                    counters[name] = counters.get(name, 0) + delta

        def by_prefix(prefix: str) -> Dict[str, int]:
            return {k[len(prefix):]: v for k, v in counters.items() if k.startswith(prefix) and v}

        monthly_calls = by_prefix("doc#")
        by_specialty = by_prefix("spec#")
        languages = by_prefix("lang#")
        emergencies = counters.get("esi_1_2", 0)
        urgent = counters.get("esi_3", 0)
        false_alarms = counters.get("esi_4_5", 0)
        users_count = self._count_users()
        avg_response = self._avg_response_seconds(cases)
        avg_accept = self._avg_seconds(cases, "offer_started_at", "accepted_at")

        demo = settings.DEMO_MODE
        if demo:
            # Presentation padding, flagged with demo_mode=True so the portal can label it.
            monthly_calls = monthly_calls or {
                "Dr. Sarah Khan, MD (Cardio)": 14,
                "Dr. Rajesh Sharma, MD (Neuro)": 9,
                "Dr. James Mitchell, FACS (Trauma)": 18,
                "Dr. Ayesha Siddiqui, FCPS (Pulmo)": 7
            }
            by_specialty = by_specialty or {"Cardiology": 14, "Neurology": 9, "Trauma": 18, "Pulmonology": 7}
            languages = languages or {"Urdu": 24, "Hindi": 15, "English": 9}
            users_count = max(users_count, 142)
            emergencies = max(emergencies, 48)
            urgent = max(urgent, 9)
            false_alarms = max(false_alarms, 12)
            avg_response = avg_response if avg_response is not None else 3.8
            avg_accept = avg_accept if avg_accept is not None else 34.0

        return AnalyticsSummary(
            total_registered_users=users_count,
            total_emergency_cases=emergencies,
            total_urgent_cases=urgent,
            total_false_alarms=false_alarms,
            monthly_doctor_calls=monthly_calls,
            cases_by_specialty=by_specialty,
            avg_response_time_seconds=avg_response,
            avg_time_to_accept_seconds=avg_accept,
            languages_handled=languages,
            demo_mode=demo,
            month=month,
        )

# Global singleton
dynamo_service = DynamoDBService()
