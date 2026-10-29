"""Human-facing broker errors, shared by API responses and session events."""
import re

# Older/local/node brokers prefix error replies with Python diagnostic types.
# Normalize only those diagnostic labels, not ordinary headings such as "GitHub:".
_EXCEPTION_LABEL = re.compile(
    r"(?:^|(?<=\())(?:(?:[A-Za-z_]\w*\.)*(?:[A-Z]\w*(?:Error|Exception)|Exception|"
    r"LaunchRefused|BrokerUnavailable|MachineOffline):\s*)+"
)


def unknown_method(error: Exception) -> bool:
    return _EXCEPTION_LABEL.sub('', str(error)).startswith('unknown method ')


def public_error(error: Exception) -> str:
    if unknown_method(error):
        return "The session service has an internal configuration or version mismatch. Ask the owner to update or repair it, then try again."
    message = re.sub(r"(?:^|(?<=\())internal:\s*(?:[A-Za-z_]\w*\.)*[A-Z]\w*(?=$|\))",
                     "The session service couldn't complete the request. Try again.", str(error))
    return _EXCEPTION_LABEL.sub('', message)

