import os
from crm_npd.bootstrap import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("CRM_NPD_HOST", "0.0.0.0") or "0.0.0.0"
    port = int(os.environ.get("CRM_NPD_PORT") or os.environ.get("PORT") or "8089")
    app.run(host=host, port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
