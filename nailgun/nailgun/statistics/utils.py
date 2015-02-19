#    Copyright 2015 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os
import random
import six

from contextlib import contextmanager

from cinderclient import client as cinder_client
from keystoneclient import discover as keystone_discover
from keystoneclient.v2_0 import client as keystone_client_v2
from keystoneclient.v3 import client as keystone_client_v3
from novaclient import client as nova_client

from nailgun import consts
from nailgun.logger import logger
from nailgun.network import manager
from nailgun import objects
from nailgun.settings import settings


collected_components_attrs = {
    "vm": {
        "attr_names": {
            "id": ["id"],
            "status": ["status"],
            "tenant_id": ["tenant_id"],
            "host_id": ["hostId"],
            "created_at": ["created"],
            "power_state": ["OS-EXT-STS:power_state"],
            "flavor_id": ["flavor", "id"],
            "image_id": ["image", "id"]
        },
        "resource_manager_path": [["nova", "servers"]]
    },
    "flavor": {
        "attr_names": {
            "id": ["id"],
            "ram": ["ram"],
            "vcpus": ["vcpus"],
            "ephemeral": ["OS-FLV-EXT-DATA:ephemeral"],
            "disk": ["disk"],
            "swap": ["swap"],
        },
        "resource_manager_path": [["nova", "flavors"]]
    },
    "tenant": {
        "attr_names": {
            "id": ["id"],
            "enabled_flag": ["enabled"],
        },
        "resource_manager_path": [["keystone", "tenants"],
                                  ["keystone", "projects"]]
    },
    "image": {
        "attr_names": {
            "id": ["id"],
            "minDisk": ["minDisk"],
            "minRam": ["minRam"],
            "sizeBytes": ["OS-EXT-IMG-SIZE:size"],
            "created_at": ["created"],
            "updated_at": ["updated"]
        },
        "resource_manager_path": [["nova", "images"]]
    },
    "volume": {
        "attr_names": {
            "id": ["id"],
            "availability_zone": ["availability_zone"],
            "encrypted_flag": ["encrypted"],
            "bootable_flag": ["bootable"],
            "status": ["status"],
            "volume_type": ["volume_type"],
            "size": ["size"],
            "host": ["os-vol-host-attr:host"],
            "snapshot_id": ["snapshot_id"],
            "attachments": ["attachments"],
            "tenant_id": ["os-vol-tenant-attr:tenant_id"],
        },
        "resource_manager_path": [["cinder", "volumes"]]
    },
}


class ClientProvider(object):
    """Initialize clients for OpenStack components
    and expose them as attributes
    """

    def __init__(self, cluster):
        self.cluster = cluster
        self._nova = None
        self._cinder = None
        self._keystone = None
        self._credentials = None

    @property
    def nova(self):
        if self._nova is None:
            self._nova = nova_client.Client(
                settings.OPENSTACK_API_VERSION["nova"],
                *self.credentials,
                service_type=consts.NOVA_SERVICE_TYPE.compute
            )

        return self._nova

    @property
    def cinder(self):
        if self._cinder is None:
            self._cinder = cinder_client.Client(
                settings.OPENSTACK_API_VERSION["cinder"],
                *self.credentials
            )

        return self._cinder

    @property
    def keystone(self):
        if self._keystone is None:
            # kwargs are universal for v2 and v3 versions of
            # keystone client that are different only in accepting
            # of tenant/project keyword name
            auth_kwargs = {
                "username": self.credentials[0],
                "password": self.credentials[1],
                "tenant_name": self.credentials[2],
                "project_name": self.credentials[2],
                "auth_url": self.credentials[3]
            }
            self._keystone = self._get_keystone_client(auth_kwargs)

        return self._keystone

    def _get_keystone_client(self, auth_creds):
        """Instantiate client based on returned from keystone
        server version data.

        :param auth_creds: credentials for authentication which also are
        parameters for client's instance initialization
        :returns: instance of keystone client of appropriate version
        :raises: exception if response from server contains version other than
        2.x and 3.x
        """
        discover = keystone_discover.Discover(**auth_creds)

        for version_data in discover.version_data():
            version = version_data["version"][0]
            if version <= 2:
                return keystone_client_v2.Client(**auth_creds)
            elif version == 3:
                return keystone_client_v3.Client(**auth_creds)

        raise Exception("Failed to discover keystone version "
                        "for auth_url {0}".format(
                            auth_creds.get("auth_url"))
                        )

    @property
    def credentials(self):
        if self._credentials is None:
            access_data = objects.Cluster.get_creds(self.cluster)

            os_user = access_data["user"]["value"]
            os_password = access_data["password"]["value"]
            os_tenant = access_data["tenant"]["value"]

            auth_host = _get_host_for_auth(self.cluster)
            auth_url = "http://{0}:{1}/{2}/".format(
                auth_host, settings.AUTH_PORT,
                settings.OPENSTACK_API_VERSION["keystone"])

            self._credentials = (os_user, os_password, os_tenant, auth_url)

        return self._credentials


def _get_host_for_auth(cluster):
    return manager.NetworkManager._get_ip_by_network_name(
        _get_online_controller(cluster),
        consts.NETWORKS.management
    ).ip_addr


def get_proxy_for_cluster(cluster):
    proxy_host = _get_online_controller(cluster).ip
    proxy_port = settings.OPENSTACK_INFO_COLLECTOR_PROXY_PORT
    proxy = "http://{0}:{1}".format(proxy_host, proxy_port)

    return proxy


def _get_online_controller(cluster):
    return filter(
        lambda node: (
            "controller" in node.roles and node.online is True),
        cluster.nodes
    )[0]


def get_info_from_os_resource_manager(client_provider, resource_name):
    resource = collected_components_attrs[resource_name]

    for resource_manager_path in resource["resource_manager_path"]:
        resource_manager = _get_nested_attr(
            client_provider,
            resource_manager_path
        )

        # use first found resource manager for attributes retrieving
        if resource_manager:
            break

    else:
        # if _get_nested_attr() returned None for all invariants
        # of resource manager attribute path we should fail
        # oswl retrieving for the resource
        raise Exception("Resource manager for {0} could not be found "
                        "by openstack client provider".format(resource_name))

    instances_list = resource_manager.list()
    resource_info = []

    for inst in instances_list:
        inst_details = {}

        for attr_name, attr_path in six.iteritems(resource["attr_names"]):
            obj_dict = \
                inst.to_dict() if hasattr(inst, "to_dict") else inst.__dict__
            inst_details[attr_name] = _get_value_from_nested_dict(
                obj_dict, attr_path
            )

        resource_info.append(inst_details)

    return resource_info


def _get_value_from_nested_dict(obj_dict, key_path):
    value = obj_dict.get(key_path[0])

    if isinstance(value, dict):
        return _get_value_from_nested_dict(value, key_path[1:])

    return value


def _get_nested_attr(obj, attr_path):
    # prevent from error in case of empty list and
    # None object
    if not all([obj, attr_path]):
        return None

    attr_name = attr_path[0]
    attr_value = getattr(obj, attr_name, None)

    # stop recursion as we already are on last level of attributes nesting
    if len(attr_path) == 1:
        return attr_value

    return _get_nested_attr(attr_value, attr_path[1:])


@contextmanager
def set_proxy(proxy):
    """Replace http_proxy environment variable for the scope
    of context execution. After exit from context old proxy value
    (if any) is restored

    :param proxy: - proxy url
    """
    proxy_old_value = None

    if os.environ.get("http_proxy"):
        proxy_old_value = os.environ["http_proxy"]
        logger.warning("http_proxy variable is already set with "
                       "value: {0}. Change to {1}. Old value "
                       "will be restored after exit from script's "
                       "execution context"
                       .format(proxy_old_value, proxy))

    os.environ["http_proxy"] = proxy

    try:
        yield
    except Exception as e:
        logger.exception("Error while talking to proxy. Details: {0}"
                         .format(six.text_type(e)))
    finally:
        if proxy_old_value:
            logger.info("Restoring old value for http_proxy")
            os.environ["http_proxy"] = proxy_old_value
        else:
            logger.info("Deleting set http_proxy environment variable")
            del os.environ["http_proxy"]


def dithered(medium, interval=(0.9, 1.1)):
    return random.randint(int(medium * interval[0]), int(medium * interval[1]))
