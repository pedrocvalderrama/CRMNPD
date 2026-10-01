import os
from crm_npd.bootstrap import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8089")), debug=os.environ.get("FLASK_DEBUG", "0") == "1")
