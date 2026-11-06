"""Describe the shared v2 error envelope using the same fields as the runtime."""
from uuid import UUID

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict


class V2Error(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str
    message: str
    retryable: bool


class V2ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra='forbid')
    error: V2Error
    request_id: UUID


def install_v2_error_contract(app: FastAPI) -> None:
    original_openapi = app.openapi

    def openapi():
        if app.openapi_schema is not None:
            return app.openapi_schema
        document = original_openapi()
        components = document.setdefault('components', {}).setdefault('schemas', {})
        envelope = V2ErrorEnvelope.model_json_schema(ref_template='#/components/schemas/{model}')
        components.update(envelope.pop('$defs', {}))
        components['V2ErrorEnvelope'] = envelope
        reference = {'$ref': '#/components/schemas/V2ErrorEnvelope'}
        response = {'description': 'Request rejected or service failure. Cache-Control: no-store.',
            'content': {'application/json': {'schema': reference}}}
        for path, item in document['paths'].items():
            if not path.startswith('/v2/'):
                continue
            for method, operation in item.items():
                if method not in {'get', 'post', 'put', 'patch', 'delete', 'options', 'head'}:
                    continue
                responses = operation.setdefault('responses', {})
                responses['default'] = response
                if '422' in responses:
                    responses['422'] = {**response, 'description': 'Input validation failed.'}
                if operation.get('security'):
                    responses.setdefault('401', {**response, 'description': 'Session or request receipt is invalid, expired or revoked.'})
                    # Security schemes supply these headers in Swagger. Keep query/
                    # pagination/body parameters, without duplicate optional auth fields.
                    operation['parameters'] = [p for p in operation.get('parameters', [])
                        if not (p.get('in') == 'header' and p['name'].lower() in {'authorization', 'x-data-receipt'})]
                    if not operation['parameters']:
                        operation.pop('parameters')
        app.openapi_schema = document
        return document

    app.openapi = openapi
