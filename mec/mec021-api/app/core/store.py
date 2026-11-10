"""In-memory stores (replace with Redis/DB for production)."""

from app.models.mec021 import AppMobilityServiceInfo, MobilityProcedureInfo, MobilityProcedureSubscription
from typing import Dict

ams_store: Dict[str, AppMobilityServiceInfo] = {}
procedure_store: Dict[str, MobilityProcedureInfo] = {}
subscription_store: Dict[str, MobilityProcedureSubscription] = {}
