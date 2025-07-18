# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See LICENSE.txt in the project root for
# license information.
# -------------------------------------------------------------------------
from __future__ import annotations
import time
from typing import TYPE_CHECKING, Any, Awaitable, Optional, cast, TypeVar, Union

from ...credentials import AccessTokenInfo, TokenRequestOptions
from ..pipeline import PipelineRequest, PipelineResponse
from ..pipeline._tools_async import await_result
from ._base_async import AsyncHTTPPolicy
from ._authentication import _BearerTokenCredentialPolicyBase
from ...rest import AsyncHttpResponse, HttpRequest
from ...utils._utils import get_running_async_lock

if TYPE_CHECKING:
    from ...credentials import AsyncTokenCredential
    from ...runtime.pipeline import PipelineRequest, PipelineResponse

AsyncHTTPResponseType = TypeVar("AsyncHTTPResponseType", bound=AsyncHttpResponse)
HTTPRequestType = TypeVar("HTTPRequestType", bound=HttpRequest)


class AsyncBearerTokenCredentialPolicy(AsyncHTTPPolicy[HTTPRequestType, AsyncHTTPResponseType]):
    """Adds a bearer token Authorization header to requests.

    :param credential: The credential.
    :type credential: ~corehttp.credentials.TokenCredential
    :param str scopes: Lets you specify the type of access needed.
    :keyword auth_flows: A list of authentication flows to use for the credential.
    :paramtype auth_flows: list[dict[str, Union[str, list[dict[str, str]]]]]
    """

    # pylint: disable=unused-argument
    def __init__(
        self,
        credential: "AsyncTokenCredential",
        *scopes: str,
        auth_flows: Optional[list[dict[str, Union[str, list[dict[str, str]]]]]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self._credential = credential
        self._lock_instance = None
        self._scopes = scopes
        self._token: Optional[AccessTokenInfo] = None
        self._auth_flows = auth_flows

    @property
    def _lock(self):
        if self._lock_instance is None:
            self._lock_instance = get_running_async_lock()
        return self._lock_instance

    async def on_request(
        self,
        request: PipelineRequest[HTTPRequestType],
        *,
        auth_flows: Optional[list[dict[str, Union[str, list[dict[str, str]]]]]] = None,
    ) -> None:
        """Adds a bearer token Authorization header to request and sends request to next policy.

        :param request: The pipeline request object to be modified.
        :type request: ~corehttp.runtime.pipeline.PipelineRequest
        :keyword auth_flows: A list of authentication flows to use for the credential.
        :paramtype auth_flows: list[dict[str, Union[str, list[dict[str, str]]]]]
        :raises: :class:`~corehttp.exceptions.ServiceRequestError`
        """
        # If auth_flows is an empty list, we should not attempt to authorize the request.
        if auth_flows is not None and len(auth_flows) == 0:
            return
        _BearerTokenCredentialPolicyBase._enforce_https(request)  # pylint:disable=protected-access

        if self._token is None or self._need_new_token:
            async with self._lock:
                # double check because another coroutine may have acquired a token while we waited to acquire the lock
                if self._token is None or self._need_new_token:
                    options: TokenRequestOptions = {"auth_flows": auth_flows} if auth_flows else {}  # type: ignore
                    self._token = await await_result(self._credential.get_token_info, *self._scopes, options=options)
        request.http_request.headers["Authorization"] = "Bearer " + cast(AccessTokenInfo, self._token).token

    async def authorize_request(self, request: PipelineRequest[HTTPRequestType], *scopes: str, **kwargs: Any) -> None:
        """Acquire a token from the credential and authorize the request with it.

        Keyword arguments are passed to the credential's get_token method. The token will be cached and used to
        authorize future requests.

        :param ~corehttp.runtime.pipeline.PipelineRequest request: the request
        :param str scopes: required scopes of authentication
        """
        options: TokenRequestOptions = {}
        # Loop through all the keyword arguments and check if they are part of the TokenRequestOptions.
        for key in list(kwargs.keys()):
            if key in TokenRequestOptions.__annotations__:  # pylint:disable=no-member
                options[key] = kwargs.pop(key)  # type: ignore[literal-required]

        async with self._lock:
            self._token = await await_result(self._credential.get_token_info, *scopes, options=options)
        request.http_request.headers["Authorization"] = "Bearer " + cast(AccessTokenInfo, self._token).token

    async def send(
        self, request: PipelineRequest[HTTPRequestType]
    ) -> PipelineResponse[HTTPRequestType, AsyncHTTPResponseType]:
        """Authorize request with a bearer token and send it to the next policy

        :param request: The pipeline request object
        :type request: ~corehttp.runtime.pipeline.PipelineRequest
        :return: The pipeline response object
        :rtype: ~corehttp.runtime.pipeline.PipelineResponse
        """
        op_auth_flows = request.context.options.pop("auth_flows", None)
        auth_flows = op_auth_flows if op_auth_flows is not None else self._auth_flows
        await await_result(self.on_request, request, auth_flows=auth_flows)
        try:
            response = await self.next.send(request)
        except Exception:
            await await_result(self.on_exception, request)
            raise
        await await_result(self.on_response, request, response)

        if response.http_response.status_code == 401:
            self._token = None  # any cached token is invalid
            if "WWW-Authenticate" in response.http_response.headers:
                request_authorized = await self.on_challenge(request, response)
                if request_authorized:
                    try:
                        response = await self.next.send(request)
                    except Exception:
                        await await_result(self.on_exception, request)
                        raise
                    await await_result(self.on_response, request, response)

        return response

    async def on_challenge(
        self,
        request: PipelineRequest[HTTPRequestType],
        response: PipelineResponse[HTTPRequestType, AsyncHTTPResponseType],
    ) -> bool:
        """Authorize request according to an authentication challenge

        This method is called when the resource provider responds 401 with a WWW-Authenticate header.

        :param ~corehttp.runtime.pipeline.PipelineRequest request: the request which elicited an authentication
            challenge
        :param ~corehttp.runtime.pipeline.PipelineResponse response: the resource provider's response
        :returns: a bool indicating whether the policy should send the request
        :rtype: bool
        """
        # pylint:disable=unused-argument
        return False

    def on_response(
        self,
        request: PipelineRequest[HTTPRequestType],
        response: PipelineResponse[HTTPRequestType, AsyncHTTPResponseType],
    ) -> Optional[Awaitable[None]]:
        """Executed after the request comes back from the next policy.

        :param request: Request to be modified after returning from the policy.
        :type request: ~corehttp.runtime.pipeline.PipelineRequest
        :param response: Pipeline response object
        :type response: ~corehttp.runtime.pipeline.PipelineResponse
        """

    def on_exception(self, request: PipelineRequest[HTTPRequestType]) -> None:
        """Executed when an exception is raised while executing the next policy.

        This method is executed inside the exception handler.

        :param request: The Pipeline request object
        :type request: ~corehttp.runtime.pipeline.PipelineRequest
        """
        # pylint: disable=unused-argument
        return

    @property
    def _need_new_token(self) -> bool:
        now = time.time()
        return (
            not self._token
            or (self._token.refresh_on is not None and self._token.refresh_on <= now)
            or (self._token.expires_on - now < 300)
        )
