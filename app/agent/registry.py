from enum import StrEnum

class ToolName(StrEnum):
    GET_PRICE              = "get_price"
    GET_STOCK              = "get_stock"
    GET_ALL_PRODUCTS       = "get_all_products"
    GET_PRODUCTS_BY_CAT    = "get_products_by_category"
    GET_HOURS              = "get_hours"
    GENERATE_PAYMENT_LINK  = "generate_payment_link"
    GET_TREATMENT_INFO     = "get_treatment_info"
    GET_PRICES_DENTIST     = "get_prices"
    GET_INSURANCES         = "get_insurances"
    GET_CURRENT_DATE_HOUR  = "get_current_date_hour"
    CHECK_AVAILABILITY     = "check_availability"
    BOOK_APPOINTMENT       = "book_appointment"
    GET_APPOINTMENT        = "get_appointment"
    CANCEL_APPOINTMENT     = "cancel_appointment"
    RESCHEDULE_APPOINTMENT = "reschedule_appointment"
    GET_AVAILABILITY_PADEL = "get_availability"
    CREATE_BOOKING_PADEL   = "create_booking"
    CANCEL_BOOKING_PADEL   = "cancel_booking"
    GENERATE_PADEL_PAYMENT = "generate_padel_payment_link"

RETAIL_TOOLS: frozenset[ToolName] = frozenset({
    ToolName.GET_PRICE,
    ToolName.GET_STOCK,
    ToolName.GET_ALL_PRODUCTS,
    ToolName.GET_PRODUCTS_BY_CAT,
    ToolName.GET_HOURS,
    ToolName.GENERATE_PAYMENT_LINK,
})

CALENDAR_TOOLS: frozenset[ToolName] = frozenset({   
    ToolName.CHECK_AVAILABILITY,
    ToolName.BOOK_APPOINTMENT,
    ToolName.GET_APPOINTMENT,
    ToolName.CANCEL_APPOINTMENT,
    ToolName.RESCHEDULE_APPOINTMENT,
    ToolName.GET_CURRENT_DATE_HOUR,
    ToolName.GET_TREATMENT_INFO,
})

DENTIST_INFO_TOOLS: frozenset[ToolName] = frozenset({
    ToolName.GET_PRICES_DENTIST,
    ToolName.GET_INSURANCES,
    ToolName.GET_TREATMENT_INFO,
})

PADEL_TOOLS: frozenset[ToolName] = frozenset({
    ToolName.GET_AVAILABILITY_PADEL,
    ToolName.CREATE_BOOKING_PADEL,
    ToolName.CANCEL_BOOKING_PADEL,
})

KNOWN_TOOL_NAMES: frozenset[str] = frozenset(t.value for t in ToolName)
