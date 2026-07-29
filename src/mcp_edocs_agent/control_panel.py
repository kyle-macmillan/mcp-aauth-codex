"""Unauthenticated localhost control panel for the three-provider demo."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from aauth_edocs import (
    FunctionDescriptor,
    MutableControllerPolicy,
    ResourceBinding,
    SentinelRegistry,
    parse_dataflow,
    register_materialization,
    serialize_dataflow,
    serialize_rule,
)
from flask import Flask, jsonify, request
from mcp_edocs_provider import (
    CatalogEntry,
    LoadedFunction,
    MutableFunctionRegistry,
    ProviderCatalog,
)

from .derived_store import DerivedPayloadStore


@dataclass(frozen=True)
class DemoProviderAdmin:
    provider_id: str
    display_name: str
    catalog: ProviderCatalog
    policy: MutableControllerPolicy
    add_document: Callable[[dict[str, Any]], CatalogEntry]
    source_agent: str
    destination_agent: str


def create_control_panel(
    providers: Mapping[str, DemoProviderAdmin],
    *,
    sentinel: SentinelRegistry,
    function_registry: MutableFunctionRegistry,
    register_function: Callable[[dict[str, Any]], LoadedFunction],
    agents: Mapping[str, str],
    derived_store: DerivedPayloadStore | None = None,
) -> Flask:
    """Build the demo-only UI and JSON adapter over reusable stores."""
    app = Flask("edocs-demo-control-panel")

    def provider(provider_id: str) -> DemoProviderAdmin:
        try:
            return providers[provider_id]
        except KeyError as error:
            raise LookupError("unknown provider") from error

    def public_function(
        descriptor: FunctionDescriptor,
    ) -> dict[str, Any]:
        return {
            "function_id": descriptor.id,
            "description": descriptor.description,
            "input_schema": descriptor.input_schema,
            "digest": descriptor.digest,
        }

    @app.get("/")
    @app.get("/demo")
    def index():
        return _HTML

    @app.get("/api/providers")
    def list_providers():
        return jsonify(
            {
                "providers": [
                    {
                        "provider_id": item.provider_id,
                        "display_name": item.display_name,
                        "source_agent": item.source_agent,
                        "destination_agent": item.destination_agent,
                    }
                    for item in providers.values()
                ]
            }
        )

    @app.get("/api/sentinel")
    def sentinel_state():
        return jsonify(
            {
                "resource_bindings": [
                    {
                        "source_agent": source,
                        "source_ps": binding.source_ps,
                        "resource_issuer": binding.resource_issuer,
                        "resource_jkt": binding.resource_jkt,
                    }
                    for source, binding in sentinel.resource_bindings.items()
                ],
                "agents": [
                    {"role": role, "agent_id": agent_id}
                    for role, agent_id in agents.items()
                ],
                "controllers": [
                    {
                        "resource_issuer": resource_issuer,
                        "edoc_id": edoc_id,
                        "controllers": list(controllers),
                    }
                    for (
                        resource_issuer,
                        edoc_id,
                    ), controllers in sentinel.controllers.items()
                ],
                "functions": [
                    {
                        "function_id": descriptor.id,
                        "description": descriptor.description,
                        "implementation_uri": descriptor.implementation_uri,
                        "digest": descriptor.digest,
                        "input_schema": descriptor.input_schema,
                        "implementation": function_registry.artifact(
                            descriptor.id
                        ),
                    }
                    for descriptor in sentinel.functions.values()
                ],
                "materialized": [
                    serialize_dataflow(flow)
                    for flow in sorted(
                        sentinel.materialized,
                        key=lambda flow: (
                            flow.source,
                            flow.document,
                            flow.function_args_hash,
                        ),
                    )
                ],
                "derived_documents": [
                    {
                        "edoc_id": derived.edoc_id,
                        "resource_uri": derived.resource_uri,
                        "producer": serialize_dataflow(derived.producer),
                        "producer_fingerprint": (
                            derived.producer_fingerprint
                        ),
                        "output_digest": derived.output_digest,
                        "custodian": derived.custodian,
                        "controllers": list(derived.controllers),
                    }
                    for derived in sentinel.derived_documents.values()
                ],
            }
        )

    @app.post("/api/sentinel/bindings")
    def register_binding():
        body = _json_object()
        required = {
            "source_agent",
            "source_ps",
            "resource_issuer",
            "resource_jkt",
        }
        if set(body) != required:
            raise ValueError(
                "binding requires source_agent, source_ps, "
                "resource_issuer, and resource_jkt"
            )
        for key in required:
            if not isinstance(body[key], str) or not body[key]:
                raise ValueError(f"{key} must be a non-empty string")
        source_agent = body["source_agent"]
        existing = sentinel.resource_bindings.get(source_agent)
        binding = ResourceBinding(
            source_ps=body["source_ps"],
            resource_issuer=body["resource_issuer"],
            resource_jkt=body["resource_jkt"],
        )
        if existing is not None and existing != binding:
            raise ValueError(
                f"source_agent already bound to a different resource: {source_agent}"
            )
        sentinel.resource_bindings[source_agent] = binding
        return jsonify(
            {
                "binding": {
                    "source_agent": source_agent,
                    "source_ps": binding.source_ps,
                    "resource_issuer": binding.resource_issuer,
                    "resource_jkt": binding.resource_jkt,
                }
            }
        ), 201

    @app.post("/api/sentinel/controllers")
    def register_controllers():
        body = _json_object()
        if set(body) != {"resource_issuer", "edoc_id", "controllers"}:
            raise ValueError(
                "controller registration requires resource_issuer, "
                "edoc_id, and controllers"
            )
        resource_issuer = body["resource_issuer"]
        edoc_id = body["edoc_id"]
        controllers = body["controllers"]
        if not isinstance(resource_issuer, str) or not resource_issuer:
            raise ValueError("resource_issuer must be a non-empty string")
        if not isinstance(edoc_id, str) or not edoc_id:
            raise ValueError("edoc_id must be a non-empty string")
        if (
            not isinstance(controllers, list)
            or not controllers
            or any(not isinstance(item, str) or not item for item in controllers)
        ):
            raise ValueError("controllers must be a non-empty string list")
        key = (resource_issuer, edoc_id)
        controller_tuple = tuple(controllers)
        existing = sentinel.controllers.get(key)
        if existing is not None and existing != controller_tuple:
            raise ValueError(
                "controllers already registered for this eDoc with a different set"
            )
        derived = sentinel.derived_documents.get(edoc_id)
        if derived is not None and tuple(derived.controllers) != controller_tuple:
            raise ValueError(
                "controllers must match the inherited derived eDoc controllers"
            )
        sentinel.controllers[key] = controller_tuple
        if derived_store is not None:
            try:
                derived_store.mark_published(edoc_id)
            except LookupError:
                pass
        return jsonify(
            {
                "controller": {
                    "resource_issuer": resource_issuer,
                    "edoc_id": edoc_id,
                    "controllers": list(controller_tuple),
                }
            }
        ), 201

    @app.get("/api/sentinel/derived/<edoc_id>")
    def get_derived(edoc_id: str):
        if derived_store is None:
            raise LookupError("derived store is unavailable")
        value = derived_store.read(edoc_id)
        registry_derived = sentinel.derived_documents.get(edoc_id)
        if registry_derived is None:
            raise LookupError(f"unknown derived eDoc: {edoc_id}")
        return jsonify(
            {
                "edoc_id": value["edoc_id"],
                "custodian": value["custodian"],
                "controllers": value["controllers"],
                "output_digest": value["output_digest"],
                "producer": value["producer"],
                "producer_fingerprint": value["producer_fingerprint"],
                "output": value["output"],
                "published": value.get("published", False),
            }
        )

    @app.post("/api/sentinel/materializations")
    def record_materialization():
        body = _json_object()
        if set(body) != {"producer", "output", "controllers"}:
            raise ValueError(
                "materialization requires producer, output, and controllers"
            )
        if not isinstance(body["output"], dict):
            raise ValueError("output must be a JSON object")
        controllers = body["controllers"]
        if (
            not isinstance(controllers, list)
            or not controllers
            or any(not isinstance(item, str) or not item for item in controllers)
        ):
            raise ValueError("controllers must be a non-empty string list")
        producer = parse_dataflow(body["producer"])
        derived = register_materialization(
            sentinel,
            producer=producer,
            output=body["output"],
            controllers=tuple(controllers),
        )
        if derived_store is not None:
            derived_store.write(derived, body["output"])
        return jsonify(
            {
                "derived_edoc_id": derived.edoc_id,
                "custodian": derived.custodian,
                "controllers": list(derived.controllers),
            }
        ), 201

    @app.get("/api/sentinel/functions")
    def list_sentinel_functions():
        descriptors = (
            registration.descriptor
            for _, registration in function_registry.items()
        )
        return jsonify(
            {
                "functions": [
                    public_function(descriptor)
                    for descriptor in sorted(
                        descriptors,
                        key=lambda item: item.id,
                    )
                ]
            }
        )

    @app.post("/api/sentinel/functions")
    def register_sentinel_function():
        body = _json_object()
        registration = register_function(body)
        descriptor = registration.descriptor
        return jsonify(
            {
                "function": {
                    "function_id": descriptor.id,
                    "description": descriptor.description,
                    "implementation_uri": descriptor.implementation_uri,
                    "digest": descriptor.digest,
                    "input_schema": descriptor.input_schema,
                    "implementation": function_registry.artifact(
                        descriptor.id
                    ),
                }
            }
        ), 201

    @app.get("/api/providers/<provider_id>/documents")
    def list_documents(provider_id: str):
        item = provider(provider_id)
        return jsonify(
            {
                "documents": [
                    entry.public_dict(include_enabled=True)
                    for entry in item.catalog.list(include_disabled=True)
                ]
            }
        )

    @app.post("/api/providers/<provider_id>/documents")
    def add_document(provider_id: str):
        item = provider(provider_id)
        body = _json_object()
        entry = item.add_document(body)
        return jsonify(
            {"document": entry.public_dict(include_enabled=True)}
        ), 201

    @app.patch("/api/providers/<provider_id>/documents/<edoc_id>")
    def update_document(provider_id: str, edoc_id: str):
        body = _json_object()
        if not body or not set(body).issubset({"title", "description"}):
            raise ValueError("only title and description may be changed")
        entry = provider(provider_id).catalog.update_metadata(
            edoc_id,
            title=body.get("title"),
            description=body.get("description"),
        )
        return jsonify(
            {"document": entry.public_dict(include_enabled=True)}
        )

    @app.put("/api/providers/<provider_id>/documents/<edoc_id>/enabled")
    def set_document_enabled(provider_id: str, edoc_id: str):
        body = _json_object()
        if set(body) != {"enabled"}:
            raise ValueError("enabled update requires one boolean field")
        entry = provider(provider_id).catalog.set_enabled(
            edoc_id, body["enabled"]
        )
        return jsonify(
            {"document": entry.public_dict(include_enabled=True)}
        )

    @app.get("/api/providers/<provider_id>/policies")
    def list_policies(provider_id: str):
        rules = provider(provider_id).policy.list_rules()
        return jsonify({"rules": [serialize_rule(rule) for rule in rules]})

    @app.post("/api/providers/<provider_id>/policies")
    def create_policy(provider_id: str):
        body = _json_object()
        if set(body) != {"target", "prerequisite"}:
            raise ValueError("policy requires target and prerequisite")
        prerequisite = body["prerequisite"]
        stored = provider(provider_id).policy.create_rule(
            parse_dataflow(body["target"]),
            parse_dataflow(prerequisite) if prerequisite is not None else None,
        )
        return jsonify({"rule": serialize_rule(stored)}), 201

    @app.put("/api/providers/<provider_id>/policies/<rule_id>")
    def replace_policy(provider_id: str, rule_id: str):
        body = _json_object()
        if set(body) != {"target", "prerequisite"}:
            raise ValueError("policy requires target and prerequisite")
        prerequisite = body["prerequisite"]
        stored = provider(provider_id).policy.replace_rule(
            rule_id,
            parse_dataflow(body["target"]),
            parse_dataflow(prerequisite) if prerequisite is not None else None,
        )
        return jsonify({"rule": serialize_rule(stored)})

    @app.delete("/api/providers/<provider_id>/policies/<rule_id>")
    def delete_policy(provider_id: str, rule_id: str):
        provider(provider_id).policy.delete_rule(rule_id)
        return "", 204

    @app.errorhandler(LookupError)
    def unknown_provider(error):
        return jsonify(error="unknown_provider", detail=str(error)), 404

    @app.errorhandler(KeyError)
    def unknown_item(error):
        return jsonify(error="not_found", detail=str(error)), 404

    @app.errorhandler(ValueError)
    @app.errorhandler(TypeError)
    def invalid_request(error):
        return jsonify(error="invalid_request", detail=str(error)), 400

    return app


def _json_object() -> dict[str, Any]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError("JSON object required")
    return body


_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>eDocs provider controls</title>
<style>
body{font:15px system-ui;max-width:1100px;margin:32px auto;padding:0 20px;color:#17202a}
nav button{margin:0 8px 16px 0}.notice{background:#fff3cd;padding:12px;border-radius:6px}
section{border:1px solid #ddd;border-radius:8px;padding:16px;margin-top:18px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px;border-bottom:1px solid #ddd}
input,select,textarea,button{font:inherit;padding:7px}textarea{width:100%;min-height:90px;box-sizing:border-box}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.status{min-height:24px;color:#176b36}
.error{color:#a11}.policy{background:#f8f9fa;padding:12px;margin:10px 0;border-radius:6px}
.policy label{display:flex;flex-direction:column;gap:4px}.policy .grid{margin-bottom:10px}
dialog{width:min(760px,90vw);border:1px solid #bbb;border-radius:8px;padding:20px}
dialog::backdrop{background:#0008}
</style>
</head>
<body>
<h1>eDocs demo controls</h1>
<p class="notice">Localhost demo only — this control panel has no authentication.</p>
<nav id="providers"></nav><main id="main"></main><p id="status" class="status"></p>
<dialog id="policy-editor"><div id="policy-editor-body"></div></dialog>
<script>
let selected,currentView;
const api=(path,options={})=>fetch(path,{headers:{'Content-Type':'application/json'},...options})
 .then(async r=>{if(!r.ok){let b=await r.json().catch(()=>({}));throw Error(b.detail||r.statusText)}
 return r.status===204?null:r.json()});
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const documentLabel=document=>typeof document==='string'?document:
 `output of ${document.output_of.function}(${document.output_of.document}) → ${document.output_of.destination}`;
function msg(text,error=false){let e=document.querySelector('#status');e.textContent=text;e.className=error?'status error':'status'}
async function load(){
 const {providers}=await api('/api/providers');let nav=document.querySelector('#providers');nav.innerHTML='';
 providers.forEach(p=>{let b=document.createElement('button');b.textContent=p.display_name;b.onclick=()=>show(p);nav.appendChild(b)});
 let sentinel=document.createElement('button');sentinel.textContent='Sentinel';sentinel.onclick=showSentinel;nav.appendChild(sentinel);
 if(providers.length)show(providers[0]);
}
async function showSentinel(){
 selected=null;let state=await api('/api/sentinel');
 document.querySelector('#main').innerHTML=`<h2>Sentinel</h2>
 <section><h3>Demo agents</h3><table><thead><tr><th>Window</th><th>Agent identity</th></tr></thead>
 <tbody>${state.agents.map(a=>`<tr><td>${esc(a.role)}</td><td><code>${esc(a.agent_id)}</code></td></tr>`).join('')}</tbody></table></section>
 <section><h3>Materialized dataflows</h3><button onclick="showSentinel()">Refresh</button>
 ${state.materialized.length?`<table><thead><tr><th>Source</th><th>Document</th><th>Function</th><th>Destination</th><th>Arguments</th></tr></thead>
 <tbody>${state.materialized.map(f=>`<tr><td>${esc(f.source)}</td><td>${esc(f.document)}</td><td>${esc(f.function)}</td>
 <td>${esc(f.destination)}</td><td><code>${esc(JSON.stringify(f.function_args))}</code></td></tr>`).join('')}</tbody></table>`:
 '<p>No dataflows have materialized yet.</p>'}</section>
 <section><h3>Derived eDocs</h3>${state.derived_documents.length?
 `<table><thead><tr><th>eDoc</th><th>Producer</th><th>Custodian</th><th>Output digest</th></tr></thead>
 <tbody>${state.derived_documents.map(d=>`<tr><td><code>${esc(d.resource_uri)}</code></td>
 <td>${esc(`${d.producer.source} → ${d.producer.function}(${d.producer.document}) → ${d.producer.destination}`)}</td>
 <td>${esc(d.custodian)}</td><td><code>${esc(d.output_digest)}</code></td></tr>`).join('')}</tbody></table>`:
 '<p>No derived eDocs have been registered yet.</p>'}</section>
 <section><h3>Authoritative controllers</h3><pre>${esc(JSON.stringify(state.controllers,null,2))}</pre></section>
 <section><h3>Resource bindings</h3><pre>${esc(JSON.stringify(state.resource_bindings,null,2))}</pre></section>
 <section><h3>Registered functions</h3><table><thead><tr><th>Function ID</th><th>Description</th><th>SQL</th></tr></thead>
 <tbody>${state.functions.map(f=>`<tr><td><code>${esc(f.function_id)}</code></td><td>${esc(f.description)}</td>
 <td><code>${esc(f.implementation?.source||'No demo artifact installed')}</code></td></tr>`).join('')}</tbody></table>
 <h4>Register executable function</h4>
 <p>Upload a schema-conforming SQL function. Registration installs it, but does not create an invocation policy.</p>
 <div class="grid"><input id="fn-id" placeholder="Function ID, e.g. summarize@1">
 <input id="fn-description" placeholder="Description"></div>
 <textarea id="fn-sql" placeholder="SELECT department, count(*) AS count FROM document GROUP BY department"></textarea>
 <textarea id="fn-schema">{"type":"object","properties":{},"additionalProperties":false}</textarea>
 <button onclick="addFunction()">Register function</button></section>`;
}
const options=(items,current,value,label)=>items.map(item=>`<option value="${esc(value(item))}" ${value(item)===current?'selected':''}>${esc(label(item))}</option>`).join('');
function policyForm(prefix,target,prerequisite,docs,state,p){
 target=target||{source:p.source_agent,function:state.functions[0]?.function_id||'',document:docs[0]?.edoc_id||'',
  destination:p.destination_agent,function_args:{}};
 let prerequisites=[...state.materialized];
 let sources=[...new Set([target.source,p.source_agent])];
 let destinations=[...new Set([target.destination,p.destination_agent])];
 if(prerequisite&&!prerequisites.some(f=>JSON.stringify(f)===JSON.stringify(prerequisite)))prerequisites.unshift(prerequisite);
 return `<div class="policy"><div class="grid">
 <label>Source<select id="${prefix}-source">${options(sources,target.source,v=>v,v=>v)}</select></label>
 <label>Document<select id="${prefix}-document">${options(docs,target.document,d=>d.edoc_id,d=>`${d.title} (${d.edoc_id})`)}</select></label>
 <label>Function<select id="${prefix}-function">${options(state.functions,target.function,f=>f.function_id,f=>`${f.function_id} — ${f.description}`)}</select></label>
 <label>Destination<select id="${prefix}-destination">${options(destinations,target.destination,v=>v,v=>v)}</select></label>
 </div><label>Exact function arguments (JSON)<textarea id="${prefix}-args">${esc(JSON.stringify(target.function_args,null,2))}</textarea></label>
 <label>Prerequisite<select id="${prefix}-prerequisite"><option value="">None</option>
 ${prerequisites.map(f=>{let value=encodeURIComponent(JSON.stringify(f));return `<option value="${value}" ${prerequisite&&JSON.stringify(f)===JSON.stringify(prerequisite)?'selected':''}>${esc(`${f.function} on ${f.document}`)}</option>`}).join('')}
 </select></label></div>`;
}
function policyPayload(prefix){
 let prerequisite=document.querySelector(`#${prefix}-prerequisite`).value;
 return {target:{source:document.querySelector(`#${prefix}-source`).value,
  function:document.querySelector(`#${prefix}-function`).value,
  document:document.querySelector(`#${prefix}-document`).value,
  destination:document.querySelector(`#${prefix}-destination`).value,
  function_args:JSON.parse(document.querySelector(`#${prefix}-args`).value)},
  prerequisite:prerequisite?JSON.parse(decodeURIComponent(prerequisite)):null};
}
async function show(p){
 selected=p;let [documents,policies,state]=await Promise.all([
 api(`/api/providers/${p.provider_id}/documents`),api(`/api/providers/${p.provider_id}/policies`),api('/api/sentinel')]);
 let docs=documents.documents;
 currentView={p,docs,policies,state};
 document.querySelector('#main').innerHTML=`<h2>${esc(p.display_name)}</h2>
 <section><h3>Files</h3><table><thead><tr><th>Title</th><th>ID</th><th>Enabled</th><th></th></tr></thead>
 <tbody>${docs.map(d=>`<tr><td>${esc(d.title)}</td><td>${esc(d.edoc_id)}</td><td>${d.enabled}</td>
 <td><button onclick="toggleDoc('${esc(d.edoc_id)}',${!d.enabled})">${d.enabled?'Disable':'Enable'}</button></td></tr>`).join('')}</tbody></table>
 <h4>Add CSV file</h4><div class="grid"><input id="title" placeholder="Title"><input id="description" placeholder="Description"></div>
 <input id="csv-file" type="file" accept=".csv,text/csv"> <button onclick="addDoc()">Add file</button></section>
 <section><h3>Available functions</h3><p>Functions come from the shared registry; invocation policy is specific to ${esc(p.display_name)}.</p>
 <table><thead><tr><th>Function ID</th><th>Description</th><th>SQL</th><th>${esc(p.display_name)} policy</th></tr></thead>
 <tbody>${state.functions.map(f=>{let count=policies.rules.filter(r=>r.target.function===f.function_id).length;
 return `<tr><td><code>${esc(f.function_id)}</code></td><td>${esc(f.description)}</td>
 <td><code>${esc(f.implementation?.source||'No demo artifact installed')}</code></td>
 <td>${count?`${count} supported dataflow${count===1?'':'s'}`:'No policy — invocation denied'}</td></tr>`}).join('')}</tbody></table></section>
 <section><h3>Policies</h3>${policies.rules.map(r=>`<div class="policy">
 <p><strong>${esc(r.target.function)}</strong> may run on <strong>${esc(documentLabel(r.target.document))}</strong><br>
 from <code>${esc(r.target.source)}</code> to <code>${esc(r.target.destination)}</code></p>
 <p>Exact arguments: <code>${esc(JSON.stringify(r.target.function_args))}</code></p>
 ${r.prerequisite?`<p>Requires: <code>${esc(r.prerequisite.function)}</code> on <code>${esc(r.prerequisite.document)}</code></p>`:''}
 ${typeof r.target.document==='string'?`<button onclick="openPolicyEditor('${esc(r.rule_id)}')">Edit</button>`:''}
 <button onclick="deleteRule('${esc(r.rule_id)}')">Delete</button></div>`).join('')}
 <h4>Add policy</h4>${policyForm('new-rule',null,null,docs,state,p)}
 <button onclick="addRule()">Add policy</button></section>`;
}
async function action(fn){try{await fn();msg('Saved');await show(selected)}catch(e){msg(e.message,true)}}
const toggleDoc=(id,enabled)=>action(()=>api(`/api/providers/${selected.provider_id}/documents/${id}/enabled`,{method:'PUT',body:JSON.stringify({enabled})}));
const addDoc=()=>action(async()=>{let file=document.querySelector('#csv-file').files[0];
 if(!file)throw Error('Choose a CSV file');
 return api(`/api/providers/${selected.provider_id}/documents`,{method:'POST',body:JSON.stringify({
 title:document.querySelector('#title').value,description:document.querySelector('#description').value,
 csv:await file.text()})})});
const deleteRule=id=>action(()=>api(`/api/providers/${selected.provider_id}/policies/${id}`,{method:'DELETE'}));
const addRule=()=>action(()=>api(`/api/providers/${selected.provider_id}/policies`,{method:'POST',body:JSON.stringify(policyPayload('new-rule'))}));
function openPolicyEditor(id){
 let {p,docs,policies,state}=currentView,r=policies.rules.find(rule=>rule.rule_id===id);
 document.querySelector('#policy-editor-body').innerHTML=`<h3>Edit ${esc(r.target.function)} policy</h3>
 ${policyForm('edit-rule',r.target,r.prerequisite,docs,state,p)}
 <button onclick="saveEditedRule('${esc(id)}')">Save changes</button>
 <button onclick="document.querySelector('#policy-editor').close()">Cancel</button>`;
 document.querySelector('#policy-editor').showModal();
}
async function saveEditedRule(id){try{
 await api(`/api/providers/${selected.provider_id}/policies/${id}`,{
  method:'PUT',body:JSON.stringify(policyPayload('edit-rule'))});
 document.querySelector('#policy-editor').close();msg('Saved');await show(selected);
 }catch(e){msg(e.message,true)}}
async function addFunction(){try{
 await api('/api/sentinel/functions',{method:'POST',body:JSON.stringify({
  function_id:document.querySelector('#fn-id').value,description:document.querySelector('#fn-description').value,
  input_schema:JSON.parse(document.querySelector('#fn-schema').value),
  implementation:{runtime:'sql',source:document.querySelector('#fn-sql').value}})});
 msg('Function registered');await showSentinel()}catch(e){msg(e.message,true)}}
load().catch(e=>msg(e.message,true));
</script></body></html>"""
