KNOWN_TOOL_NAMES: frozenset[str] = frozenset({
    # Retail / bakery
    "get_price",
    "get_stock",
    "get_all_products",
    "get_products_by_category",
    "generate_payment_link",
    # Shared (no tool_config gate)
    "get_hours",
    "get_current_date_hour",
    # Calendar / dentist
    "check_availability",
    "book_appointment",
    "get_appointment",
    "cancel_appointment",
    "reschedule_appointment",
    "get_treatment_info",
    "get_prices",
    "get_insurances",
    # Padel
    "get_availability",
    "create_booking",
    "cancel_booking",
    "generate_padel_payment_link",
    # Reseller
    "get_reseller",
})
