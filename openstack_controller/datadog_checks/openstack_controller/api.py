# (C) Datadog, Inc. 2018
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
import requests
import simplejson as json
from six.moves.urllib.parse import urljoin
from .settings import (DEFAULT_API_REQUEST_TIMEOUT, DEFAULT_KEYSTONE_API_VERSION, DEFAULT_NEUTRON_API_VERSION,
                       DEFAULT_PAGINATED_LIMIT, DEFAULT_MAX_RETRY)
from .exceptions import (InstancePowerOffFailure, AuthenticationNeeded, KeystoneUnreachable)


UNSCOPED_AUTH = 'unscoped'


class AbstractApi(object):

    def __init__(self, logger, endpoint, auth_token, timeout=DEFAULT_API_REQUEST_TIMEOUT, ssl_verify=False,
                 proxies=None):
        self.logger = logger
        self.ssl_verify = ssl_verify
        self.proxies = proxies
        self.endpoint = endpoint
        self.auth_token = auth_token
        self.headers = {'X-Auth-Token': auth_token}
        self.timeout = timeout
        # Cache for the `_make_request` method
        self.cache = {}

    def get_endpoint(self):
        self._make_request(self.endpoint, self.headers)

    def _make_request(self, url, headers, params=None):
        """
        Generic request handler for OpenStack API requests
        Raises specialized Exceptions for commonly encountered error codes
        """
        self.logger.debug("Request URL, Headers and Params: %s, %s, %s", url, headers, params)

        # Checking if request is in cache
        cache_key = "|".join([url, json.dumps(headers), json.dumps(params), str(self.timeout)])
        if cache_key in self.cache:
            self.logger.debug("Request found in cache. cache key %s", cache_key)
            return self.cache.get(cache_key)

        try:
            resp = requests.get(
                url,
                headers=headers,
                verify=self.ssl_verify,
                params=params,
                timeout=self.timeout,
                proxies=self.proxies,
            )
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            self.logger.debug("Error contacting openstack endpoint: %s", e)
            if resp.status_code == 401:
                self.logger.info('Need to reauthenticate before next check')
                raise AuthenticationNeeded()
            elif resp.status_code == 409:
                raise InstancePowerOffFailure()
            else:
                raise e
        jresp = resp.json()
        self.logger.debug("url: %s || response: %s", url, jresp)

        # Adding response to the cache
        self.cache[cache_key] = jresp
        return jresp


class ComputeApi(AbstractApi):
    def __init__(self, logger, endpoint, auth_token, timeout=DEFAULT_API_REQUEST_TIMEOUT, ssl_verify=False,
                 proxies=None, limit=DEFAULT_PAGINATED_LIMIT):
        super(ComputeApi, self).__init__(logger, endpoint, auth_token, timeout=timeout, ssl_verify=ssl_verify,
                                         proxies=proxies)
        self.paginated_limit = limit

    def get_os_hypervisor_uptime(self, hyp_id):
        url = '{}/os-hypervisors/{}/uptime'.format(self.endpoint, hyp_id)
        resp = self._make_request(url, self.headers)
        return resp.get('hypervisor', {}).get('uptime')

    def get_os_aggregates(self):
        url = '{}/os-aggregates'.format(self.endpoint)
        aggregate_list = self._make_request(url, self.headers)
        return aggregate_list.get('aggregates', [])

    def get_os_hypervisors_detail(self):
        url = '{}/os-hypervisors/detail'.format(self.endpoint)
        hypervisors = self._make_request(url, self.headers)
        return hypervisors.get('hypervisors', [])

    def get_servers_detail(self, query_params):
        url = '{}/servers/detail'.format(self.endpoint)
        return self._get_paginated_list(url, 'servers', query_params)

    def get_server_diagnostics(self, server_id):
        url = '{}/servers/{}/diagnostics'.format(self.endpoint, server_id)
        return self._make_request(url, self.headers)

    def get_project_limits(self, tenant_id):
        url = '{}/limits'.format(self.endpoint)
        server_stats = self._make_request(url, self.headers, params={"tenant_id": tenant_id})
        limits = server_stats.get('limits', {}).get('absolute', {})
        return limits

    def get_flavors_detail(self, query_params):
        url = '{}/flavors/detail'.format(self.endpoint)
        return self._get_paginated_list(url, 'flavors', query_params)

    def _get_paginated_list(self, url, obj, query_params):
        result = []
        query_params = query_params or {}
        query_params['limit'] = self.paginated_limit
        resp = self._make_request(url, self.headers, params=query_params)
        result.extend(resp.get(obj, []))
        # Avoid the extra request since we know we're done when the response has anywhere between
        # 0 and paginated_server_limit servers
        while len(resp) == self.paginated_limit:
            query_params['marker'] = resp[-1]['id']
            query_params['limit'] = self.paginated_limit
            retry = 0
            while retry < DEFAULT_MAX_RETRY:
                # `details` endpoints are typically expensive calls,
                # If it fails, we retry DEFAULT_RETRY times while reducing the `limit` param
                # otherwise we will backoff
                try:
                    resp = self._make_request(url, self.headers, params=query_params)
                    result.extend(resp.get(obj, []))

                    break
                except Exception as e:
                    query_params['limit'] /= 2
                    retry += 1
                    if retry == DEFAULT_MAX_RETRY:
                        raise e

        return result


class NeutronApi(AbstractApi):
    def __init__(self, logger, endpoint, auth_token, timeout=DEFAULT_API_REQUEST_TIMEOUT, ssl_verify=False,
                 proxies=None):
        super(NeutronApi, self).__init__(logger, endpoint, auth_token, timeout=timeout, ssl_verify=ssl_verify,
                                         proxies=proxies)

    def get_networks(self):
        url = '{}/{}/networks'.format(self.endpoint, DEFAULT_NEUTRON_API_VERSION)

        try:
            networks = self._make_request(url, self.headers)
            return networks.get('networks')
        except Exception as e:
            self.logger.warning('Unable to get the list of all network ids: {}'.format(e))
            raise e


class KeystoneApi(AbstractApi):
    def __init__(self, logger, endpoint, auth_token, timeout=DEFAULT_API_REQUEST_TIMEOUT, ssl_verify=False,
                 proxies=None):
        super(KeystoneApi, self).__init__(logger, endpoint, auth_token, timeout=timeout, ssl_verify=ssl_verify,
                                          proxies=proxies)

    def post_auth_token(self, identity, scope=UNSCOPED_AUTH):
        auth_url = urljoin(self.endpoint, "{}/auth/tokens".format(DEFAULT_KEYSTONE_API_VERSION))
        try:
            payload = {'auth': {'identity': identity, 'scope': scope}}
            headers = {'Content-Type': 'application/json'}

            resp = requests.post(
                auth_url,
                headers=headers,
                data=json.dumps(payload),
                verify=self.ssl_verify,
                timeout=DEFAULT_API_REQUEST_TIMEOUT,
                proxies=self.proxies,
            )
            resp.raise_for_status()
            self.logger.debug("url: %s || response: %s", auth_url, resp.json())
            return resp

        except (requests.exceptions.HTTPError, requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            msg = "Failed keystone auth with identity:{identity} scope:{scope} @{url}".format(
                identity=identity,
                scope=scope,
                url=auth_url)
            self.logger.debug(msg)
            raise KeystoneUnreachable(msg)

    def get_auth_projects(self):
        auth_url = ""
        try:
            auth_url = urljoin(self.endpoint, "{}/auth/projects".format(DEFAULT_KEYSTONE_API_VERSION))
            resp = requests.get(
                auth_url,
                headers=self.headers,
                verify=self.ssl_verify,
                timeout=DEFAULT_API_REQUEST_TIMEOUT,
                proxies=self.proxies
            )
            resp.raise_for_status()
            jresp = resp.json()
            self.logger.debug("url: %s || response: %s", auth_url, jresp)
            projects = jresp.get('projects')
            return projects
        except (requests.exceptions.HTTPError, requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            msg = "unable to retrieve project list from keystone auth with identity: @{url}: {ex}".format(
                    url=auth_url,
                    ex=e)
            self.logger.debug(msg)
            raise KeystoneUnreachable(msg)

    def get_projects(self, project_token):
        """
        Returns all projects in the domain
        """
        url = urljoin(self.endpoint, "{}/{}".format(DEFAULT_KEYSTONE_API_VERSION, "projects"))
        headers = {'X-Auth-Token': project_token}
        try:
            r = self._make_request(url, headers)
            return r.get('projects', [])

        except Exception as e:
            self.logger.warning('Unable to get projects: {}'.format(e))
            raise e
