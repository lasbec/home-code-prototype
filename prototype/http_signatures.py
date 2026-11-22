"""Language signatures for buffered HTTP host operations."""

from dataclasses import replace

from .syntax import (
    GUARANTEES, HOST_ERROR, HTTP_CLIENT, HTTP_SERVER, STRING, THREADS, UINT, UNIT, Type,
    builtin, result_type,
)

HEADER = Type("HttpHeader")
REQUEST = Type("HttpRequest")
RESPONSE = Type("HttpResponse")
INCOMING = Type("HttpIncoming")
HEADERS = Type("list", (HEADER,))
MILLISECONDS = Type("uint32", units=(("ms", 1),))

HTTP_RECORDS = {
    shape: builtin(shape.name, fields, shape, GUARANTEES)
    for shape, fields in (
        (HEADER, (("name", STRING), ("value", STRING))),
        (REQUEST, (("method", STRING), ("path", STRING), ("headers", HEADERS), ("body", STRING))),
        (RESPONSE, (("status", UINT), ("headers", HEADERS), ("body", STRING))),
        (INCOMING, (("id", UINT), ("request", REQUEST))),
    )
}
RESPOND = builtin("respondHttp", (("server", HTTP_SERVER), ("incoming", INCOMING),
                                 ("response", RESPONSE), ("timeout", MILLISECONDS)),
                  result_type(UNIT, HOST_ERROR))
RESPOND = replace(RESPOND, parameters=tuple(
    replace(parameter, owning=True) if parameter.name == "incoming" else parameter
    for parameter in RESPOND.parameters
))
HTTP_FUNCTIONS = {function.name: function for function in (
    builtin("requestHttp", (("client", HTTP_CLIENT), ("request", REQUEST), ("timeout", MILLISECONDS)),
            result_type(RESPONSE, HOST_ERROR)),
    builtin("acceptHttp", (("server", HTTP_SERVER), ("timeout", MILLISECONDS)),
            result_type(INCOMING, HOST_ERROR)),
    RESPOND,
)}
SLEEP = builtin("sleep", (("threads", THREADS), ("duration", MILLISECONDS)), UNIT)
