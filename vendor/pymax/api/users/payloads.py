from pymax.api.models import CamelModel

from .enums import ContactAction


class FetchContactsPayload(CamelModel):
    contact_ids: list[int]


class SearchByPhonePayload(CamelModel):
    phone: str


class ContactActionPayload(CamelModel):
    contact_id: int
    action: ContactAction
