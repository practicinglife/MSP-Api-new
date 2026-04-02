"""
Sample report data for the MSP reconciliation system.

Each record represents a device observed across multiple platforms.
Fields:
  - hostname      : device name
  - company       : company the device belongs to
  - In_ASIO       : 'Yes' / 'No' – whether device is in Atera (ASIO)
  - In_CW_PSA     : 'Yes' / 'No' – whether device is in ConnectWise PSA
  - In_S1         : 'Yes' / 'No' – whether device is in SentinelOne
  - In_SC         : 'Yes' / 'No' – whether device is in ScreenConnect
  - endpoint_protection : version string from the endpoint protection agent,
                          e.g. "SentinelOne 25.1.4.434", or "" if absent.
"""

DEVICES = [
    # --- Acme Corp ---
    {
        "hostname": "ACME-PC-01",
        "company": "Acme Corp",
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "Yes",
        "In_SC": "Yes",
        "endpoint_protection": "SentinelOne 25.1.4.434",
    },
    {
        "hostname": "ACME-PC-02",
        "company": "Acme Corp",
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "Yes",
        "In_SC": "Yes",
        "endpoint_protection": "SentinelOne 25.1.3.100",
    },
    {
        "hostname": "ACME-PC-03",
        "company": "Acme Corp",
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "Yes",
        "In_SC": "Yes",
        "endpoint_protection": "SentinelOne 25.1.4.434",
    },
    # --- Globex Industries ---
    # HEATHER-LAPTOP: in asio | connectwise_psa | sentinelone — missing ScreenConnect
    # Old logic: 3 platforms → "full" (wrong)
    # New logic: missing screenconnect → "partial" (mismatched)
    {
        "hostname": "HEATHER-LAPTOP",
        "company": "Globex Industries",
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "Yes",
        "In_SC": "No",
        "endpoint_protection": "SentinelOne 25.1.4.434",
    },
    # LAPTOP-O577LVAU: only in asio | connectwise_psa — missing S1 and SC
    # Old logic: 2 platforms → "partial" counted as matched (wrong)
    # New logic: missing sentinelone and screenconnect → "partial" (mismatched)
    {
        "hostname": "LAPTOP-O577LVAU",
        "company": "Globex Industries",
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "No",
        "In_SC": "No",
        "endpoint_protection": "",
    },
    {
        "hostname": "GLOBEX-WS-01",
        "company": "Globex Industries",
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "Yes",
        "In_SC": "Yes",
        "endpoint_protection": "SentinelOne 25.1.4.434",
    },
    {
        "hostname": "GLOBEX-WS-02",
        "company": "Globex Industries",
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "Yes",
        "In_SC": "Yes",
        "endpoint_protection": "SentinelOne 25.1.3.100",
    },
    # --- Initech LLC ---
    {
        "hostname": "INITECH-PC-01",
        "company": "Initech LLC",
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "Yes",
        "In_SC": "Yes",
        "endpoint_protection": "SentinelOne 25.1.4.434",
    },
    {
        "hostname": "INITECH-PC-02",
        "company": "Initech LLC",
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "Yes",
        "In_SC": "Yes",
        "endpoint_protection": "SentinelOne 25.1.4.434",
    },
    {
        "hostname": "INITECH-PC-03",
        "company": "Initech LLC",
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "Yes",
        "In_SC": "No",
        "endpoint_protection": "SentinelOne 25.1.4.434",
    },
    {
        "hostname": "INITECH-PC-04",
        "company": "Initech LLC",
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "Yes",
        "In_SC": "Yes",
        "endpoint_protection": "SentinelOne 25.1.2.87",
    },
]
