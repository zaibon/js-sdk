from jumpscale.sals.vdc.models import VDCFACTORY
from beaker.middleware import SessionMiddleware
from bottle import Bottle, HTTPResponse
from jumpscale.loader import j
from jumpscale.packages.auth.bottle.auth import SESSION_OPTS, login_required, get_user_info
from jumpscale.packages.marketplace.bottle.models import UserEntry
from jumpscale.core.base import StoredFactory

app = Bottle()


@app.route("/api/vdcs", method="GET")
@login_required
def list_vdcs():
    user_info = j.data.serializers.json.loads(get_user_info())
    username = user_info["username"]
    result = []
    for vdc in VDCFACTORY.list(username):
        vdc_dict = vdc.to_dict()
        vdc_dict.pop("s3")
        vdc_dict.pop("kubernetes")
        result.append(vdc_dict)
    return HTTPResponse(
        j.data.serializers.json.dumps(result), status=200, headers={"Content-Type": "application/json"},
    )


@app.route("/api/vdcs/<name>", method="GET")
@login_required
def get_vdc_info(name):
    user_info = j.data.serializers.json.loads(get_user_info())
    username = user_info["username"]
    vdc = VDCFACTORY.find(vdc_name=name, owner_tname=username, load_info=True)
    if not vdc:
        return HTTPResponse(status=404, headers={"Content-Type": "application/json"},)
    return HTTPResponse(
        j.data.serializers.json.dumps(vdc.to_dict()), status=200, headers={"Content-Type": "application/json"},
    )


@app.route("/api/allowed", method="GET")
@login_required
def allowed():
    user_factory = StoredFactory(UserEntry)
    user_info = j.data.serializers.json.loads(get_user_info())
    tname = user_info["username"]
    explorer_url = j.core.identity.me.explorer.url
    instances = user_factory.list_all()
    for name in instances:
        user_entry = user_factory.get(name)
        if user_entry.tname == tname and user_entry.explorer_url == explorer_url and user_entry.has_agreed:
            return j.data.serializers.json.dumps({"allowed": True})
    return j.data.serializers.json.dumps({"allowed": False})


@app.route("/api/accept", method="GET")
@login_required
def accept():
    user_factory = StoredFactory(UserEntry)

    user_info = j.data.serializers.json.loads(get_user_info())
    tname = user_info["username"]
    explorer_url = j.core.identity.me.explorer.url

    if "testnet" in explorer_url:
        explorer_name = "testnet"
    elif "devnet" in explorer_url:
        explorer_name = "devnet"
    elif "explorer.grid.tf" in explorer_url:
        explorer_name = "mainnet"
    else:
        return HTTPResponse(
            j.data.serializers.json.dumps({"error": f"explorer {explorer_url} is not supported"}),
            status=500,
            headers={"Content-Type": "application/json"},
        )

    user_entry = user_factory.get(f"{explorer_name}_{tname.replace('.3bot', '')}")
    if user_entry.has_agreed:
        return HTTPResponse(
            j.data.serializers.json.dumps({"allowed": True}), status=200, headers={"Content-Type": "application/json"}
        )
    else:
        user_entry.has_agreed = True
        user_entry.explorer_url = explorer_url
        user_entry.tname = tname
        user_entry.save()
        return HTTPResponse(
            j.data.serializers.json.dumps({"allowed": True}), status=201, headers={"Content-Type": "application/json"}
        )


app = SessionMiddleware(app, SESSION_OPTS)