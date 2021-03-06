# Copyright 2014 Google Inc. All rights reserved.
#
# Modifications Copyright (C) 2018 HeiGIT, University of Heidelberg.
#
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.
#

"""
Core client functionality, common across all API requests.
"""

from datetime import datetime
from datetime import timedelta
import functools
import requests
import random
import time
import collections

import openrouteservice

try: # Python 3
    from urllib.parse import urlencode
except ImportError: # Python 2
    from urllib import urlencode


_USER_AGENT = "ORSClientPython.v{}".format(openrouteservice.__version__)
_DEFAULT_BASE_URL = "https://api.openrouteservice.org"

_RETRIABLE_STATUSES = set([503])

class Client(object):
    """Performs requests to the ORS API services."""

    def __init__(self, key=None,
                 base_url=_DEFAULT_BASE_URL, 
                 timeout=None, 
                 retry_timeout=60, 
                 requests_kwargs=None,
                 queries_per_minute=40,
                 retry_over_query_limit=False):
        """
        :param key: ORS API key. Required.
        :type key: string

        :param base_url: The base URL for the request. Defaults to the ORS API
            server. Should not have a trailing slash.
        :type base_url: string

        :param timeout: Combined connect and read timeout for HTTP requests, in
            seconds. Specify "None" for no timeout.
        :type timeout: int

        :param retry_timeout: Timeout across multiple retriable requests, in
            seconds.
        :type retry_timeout: int

        :param queries_per_minute: Number of queries per second permitted.
            If the rate limit is reached, the client will sleep for the
            appropriate amount of time before it runs the current query.
            Note, it won't help to initiate another client. This saves you the 
            trouble of raised exceptions.
        :type queries_per_second: int

        :param requests_kwargs: Extra keyword arguments for the requests
            library, which among other things allow for proxy auth to be
            implemented. See the official requests docs for more info:
            http://docs.python-requests.org/en/latest/api/#main-interface
        :type requests_kwargs: dict
        """

        self.session = requests.Session()
        self.key = key
        self.base_url = base_url
        
        self.timeout = timeout
        self.retry_over_query_limit = retry_over_query_limit
        self.retry_timeout = timedelta(seconds=retry_timeout)
        self.requests_kwargs = requests_kwargs or {}
        self.requests_kwargs.update({
            "headers": {"User-Agent": _USER_AGENT,
                        'Content-type': 'application/json'},
            "timeout": self.timeout
        })

        self.queries_per_minute = queries_per_minute
        self.sent_times = collections.deque("", queries_per_minute)

    def request(self, 
                 url, params, 
                 first_request_time=None, 
                 retry_counter=0,
                 requests_kwargs=None, 
                 post_json=None,
                 dry_run=None):
        """Performs HTTP GET/POST with credentials, returning the body as
        JSON.

        :param url: URL path for the request. Should begin with a slash.
        :type url: string

        :param params: HTTP GET parameters.
        :type params: dict or list of key/value tuples

        :param first_request_time: The time of the first request (None if no
            retries have occurred).
        :type first_request_time: datetime.datetime

        :param retry_counter: The number of this retry, or zero for first attempt.
        :type retry_counter: int

        :param requests_kwargs: Same extra keywords arg for requests as per
            __init__, but provided here to allow overriding internally on a
            per-request basis.
        :type requests_kwargs: dict

        :param post_json: HTTP POST parameters. Only specified by calling method.
        :type post_json: dict

        :param dry_run: If 'true', only prints URL and parameters. 'true' or 'false'.
        :type dry_run: string

        :raises ApiError: when the API returns an error.
        :raises Timeout: if the request timed out.
        :raises TransportError: when something went wrong while trying to
            execute a request.
            
        :rtype: dict from JSON response.
        """

        if not first_request_time:
            first_request_time = datetime.now()

        elapsed = datetime.now() - first_request_time
        if elapsed > self.retry_timeout:
            raise openrouteservice.exceptions.Timeout()

        if retry_counter > 0:
            # 0.5 * (1.5 ^ i) is an increased sleep time of 1.5x per iteration,
            # starting at 0.5s when retry_counter=1. The first retry will occur
            # at 1, so subtract that first.
            delay_seconds = 1.5 ** (retry_counter - 1)

            # Jitter this value by 50% and pause.
            time.sleep(delay_seconds * (random.random() + 0.5))

        authed_url = self._generate_auth_url(url,
                                             params,
                                             )

        # Default to the client-level self.requests_kwargs, with method-level
        # requests_kwargs arg overriding.
        requests_kwargs = requests_kwargs or {}
        final_requests_kwargs = dict(self.requests_kwargs, **requests_kwargs)

        # Check if the time of the nth previous query (where n is
        # queries_per_second) is under a second ago - if so, sleep for
        # the difference.
        if self.sent_times and len(self.sent_times) == self.queries_per_minute:
            elapsed_since_earliest = time.time() - self.sent_times[0]
            if elapsed_since_earliest < 60:
                print("Request limit of {} per minute exceeded. Wait for {} seconds".format(self.queries_per_minute, 
                                                                                              60 - elapsed_since_earliest))
                time.sleep(60 - elapsed_since_earliest)
        
        # Determine GET/POST.
        # post_json is so far only sent from matrix call
        requests_method = self.session.get
        
        if post_json is not None:
            requests_method = self.session.post
            final_requests_kwargs["json"] = post_json
        
        # Only print URL and parameters for dry_run
        if dry_run:
            print("url:\n{}\nParameters:\n{}".format(self.base_url+authed_url,
                                                    final_requests_kwargs))
            return
        
        try:
            response = requests_method(self.base_url + authed_url,
                                       **final_requests_kwargs)
        except requests.exceptions.Timeout:
            raise openrouteservice.exceptions.Timeout()
        except Exception as e:
            raise openrouteservice.exceptions.TransportError(e)

        if response.status_code in _RETRIABLE_STATUSES:
            # Retry request.
            print('Server down.\nRetrying for the {}th time.'.format(retry_counter + 1))
            
            return self.request(url, params, first_request_time,
                                 retry_counter + 1, requests_kwargs, post_json)

        try:
            result = self._get_body(response)
            self.sent_times.append(time.time())
            return result
        except openrouteservice.exceptions._RetriableRequest as e:
            if isinstance(e, openrouteservice.exceptions._OverQueryLimit) and not self.retry_over_query_limit:
                raise
            
            print('Rate limit exceeded.\nRetrying for the {}th time.'.format(retry_counter + 1))
            # Retry request.
            return self.request(url, params, first_request_time,
                                 retry_counter + 1, requests_kwargs,
                                 post_json)
        except:
            raise


    def _get_body(self, response):        
        body = response.json()
#        error = body.get('error')
        status_code = response.status_code
        
        if status_code == 429:
            raise openrouteservice.exceptions._OverQueryLimit(
                str(status_code), body)
        if status_code != 200:
            raise openrouteservice.exceptions.ApiError(status_code,
                                                       body)

        return body


    def _generate_auth_url(self, path, params):
        """Returns the path and query string portion of the request URL, first
        adding any necessary parameters.

        :param path: The path portion of the URL.
        :type path: string

        :param params: URL parameters.
        :type params: dict or list of key/value tuples

        :rtype: string

        """
        
        if type(params) is dict:
            params = sorted(dict(**params).items())
        
        # Only auto-add API key when using ORS. If own instance, API key must
        # be explicitly added to params
        if self.key:
            params.append(("api_key", self.key))
            return path + "?" + _urlencode_params(params)
        elif self.base_url != _DEFAULT_BASE_URL:
            return path + "?" + _urlencode_params(params)

        raise ValueError("No API key specified. "
                         "Visit https://go.openrouteservice.org/dev-dashboard/ "
                         "to create one.")


from openrouteservice.directions import directions
from openrouteservice.distance_matrix import distance_matrix
from openrouteservice.isochrones import isochrones
from openrouteservice.geocoding import geocode
from openrouteservice.geocoding import reverse_geocode
from openrouteservice.geocode import pelias_search
from openrouteservice.geocode import pelias_structured
from openrouteservice.geocode import pelias_reverse
from openrouteservice.places import places


def _make_api_method(func):
    """
    Provides a single entry point for modifying all API methods.
    For now this is limited to allowing the client object to be modified
    with an `extra_params` keyword arg to each method, that is then used
    as the params for each web service request.

    Please note that this is an unsupported feature for advanced use only.
    It's also currently incompatibile with multiple threads, see GH #160.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        args[0]._extra_params = kwargs.pop("extra_params", None)
        result = func(*args, **kwargs)
        try:
            del args[0]._extra_params
        except AttributeError:
            pass
        return result
    return wrapper


Client.directions = _make_api_method(directions)
Client.distance_matrix = _make_api_method(distance_matrix)
Client.isochrones = _make_api_method(isochrones)
Client.geocode = _make_api_method(geocode)
Client.reverse_geocode = _make_api_method(reverse_geocode)
Client.pelias_search = _make_api_method(pelias_search)
Client.pelias_structured = _make_api_method(pelias_structured)
Client.pelias_reverse = _make_api_method(pelias_reverse)
Client.places = _make_api_method(places)


def _urlencode_params(params):
    """URL encodes the parameters.

    :param params: The parameters
    :type params: list of key/value tuples.

    :rtype: string
    """
    # urlencode does not handle unicode strings in Python 2.
    # Firstly, normalize the values so they get encoded correctly.
    params = [(key, _normalize_for_urlencode(val)) for key, val in params]
    # Secondly, unquote unreserved chars which are incorrectly quoted
    # by urllib.urlencode, causing invalid auth signatures. See GH #72
    # for more info.
    return requests.utils.unquote_unreserved(urlencode(params))


try:
    unicode
    # NOTE(cbro): `unicode` was removed in Python 3. In Python 3, NameError is
    # raised here, and caught below.

    def _normalize_for_urlencode(value):
        """(Python 2) Converts the value to a `str` (raw bytes)."""
        if isinstance(value, unicode):
            return value.encode('utf8')

        if isinstance(value, str):
            return value

        return _normalize_for_urlencode(str(value))

except NameError:
    def _normalize_for_urlencode(value):
        """(Python 3) No-op."""
        # urlencode in Python 3 handles all the types we are passing it.
        return value
