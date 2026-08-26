# App Attack Live Collector

Headless Playwright service for App Attack Live. It logs into TRAY, runs today's Menu Mix report one store at a time, downloads the CSV, and returns the Appetizers `% of Sale`.

## API

`POST /fetch-appetizers`

```json
{
  "email": "name@company.com",
  "password": "TRAY password",
  "stores": ["3231", "4445"]
}
```

Credentials are kept only in request memory and are not written to disk or logs.
