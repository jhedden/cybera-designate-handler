# v4 handler

from oslo_config import cfg
from oslo_log import log as logging

from designate.objects import Record
from designate.notification_handler.base import BaseAddressHandler
from designate.context import DesignateContext
from designate.central import rpcapi as central_api

from keystoneauth1.identity import v2
from keystoneauth1 import session
from keystoneclient.v2_0 import client as keystone_c
from novaclient.v2 import client as nova_c
from designateclient.v2 import client as designate_c

import ipaddress

LOG = logging.getLogger(__name__)

cfg.CONF.register_group(cfg.OptGroup(
    name='handler:nova_floating',
    title="Configuration for Nova notification handler for floating v4 IPs"
))

cfg.CONF.register_opts([
    cfg.ListOpt('notification-topics', default=['notifications']),
    cfg.StrOpt('control-exchange', default='nova'),
    cfg.StrOpt('zone-id'),
    cfg.StrOpt('auth-uri'),
    cfg.StrOpt('admin-tenant-id')
], group='handler:nova_floating')


class NovaFloatingHandler(BaseAddressHandler):
    """Handler for Nova's notifications"""
    __plugin_name__ = 'nova_floating'

    def get_exchange_topics(self):
        exchange = cfg.CONF[self.name].control_exchange
        topics = [topic for topic in cfg.CONF[self.name].notification_topics]

        return (exchange, topics)

    def get_event_types(self):
        return [
            'network.floating_ip.disassociate',
            'network.floating_ip.associate',
        ]

    def process_notification(self, context, event_type, payload):
        # Take notification and create a record
        LOG.debug('FloatingV4Handler: Event type received: %s', event_type)
        zone = self.get_zone(cfg.CONF[self.name].zone_id)

        domain_id = zone['id']

        # Gather extra information (need to compare payload['floating_ip'] and figure out which zone it fits in.
        # The domains are owned by admin so we need admin context?

        elevated_context = DesignateContext.get_admin_context(all_tenants=True, edit_managed_records=True)

        criterion = {
            "tenant_id": cfg.CONF[self.name].admin_tenant_id,
        }

        zones = self.central_api.find_zones(elevated_context, criterion)

        # Calculate Reverse Address
        v4address = ipaddress.ip_address(payload['floating_ip'])
        reverse_address = v4address.reverse_pointer + '.'
        reverse_id = None

        for i in zones:
            if i.name == reverse_address[4:]:
                reverse_id = i.id

        if event_type == 'network.floating_ip.associate':
            LOG.debug('FloatingV4Handler: Creating A record for %s on %s', payload['floating_ip'], payload['instance_id'])

            # Get ec2id for hostname (in user's context)
            kc = keystone_c.Client(token=context['auth_token'],
                    tenant_id=context['tenant'],
                    auth_url=cfg.CONF['handler:nova_floating'].auth_uri)

            nova_endpoint = kc.service_catalog.url_for(service_type='compute',
                        endpoint_type='internalURL')

            nvc = nova_c.Client(auth_token=kc.auth_token,
                        tenant_id=kc.auth_tenant_id,
                        bypass_url=nova_endpoint)

            server_info = nvc.servers.get(payload['instance_id'])


            # Determine the hostname
            ec2id = getattr(server_info, 'OS-EXT-SRV-ATTR:instance_name')
            ec2id = ec2id.split('-', 1)[1].lstrip('0')
            hostname = '%s.%s' % (ec2id, zone['name'])

            record_type = 'A'

            recordset_values = {
                'zone_id': domain_id,
                'name': hostname,
                'type': record_type
            }

            recordset = self._find_or_create_recordset(elevated_context, **recordset_values)

            record_values = {
                'data': payload['floating_ip'],
                'managed': True,
                'managed_plugin_name': self.get_plugin_name(),
                'managed_plugin_type': self.get_plugin_type(),
                'managed_resource_type': 'instance',
                'managed_resource_id': payload['instance_id']
            }

            LOG.debug('Creating record in %s / %s with values %r' %
                      (domain_id, recordset['id'], record_values))
            self.central_api.create_record(elevated_context,
                                       domain_id,
                                       recordset['id'],
                                       Record(**record_values))

            # Reverse Record

            record_type = 'PTR'

            if reverse_id == None:
                LOG.debug('UNABLE TO DETERMINE REVERSE ZONE: %s', payload['floating_ip'])

            else:
                recordset_values = {
                    'zone_id': reverse_id,
                    'name': reverse_address,
                    'type': record_type
                }

                recordset = self._find_or_create_recordset(elevated_context, **recordset_values)
                record_values = {
                    'data': hostname,
                    'managed': True,
                    'managed_plugin_name': self.get_plugin_name(),
                    'managed_plugin_type': self.get_plugin_type(),
                    'managed_resource_type': 'instance',
                    'managed_resource_id': payload['instance_id']
                }

                LOG.debug('Creating record in %s / %s with values %r' %
                          (reverse_id, recordset['id'], record_values))
                self.central_api.create_record(elevated_context,
                                           reverse_id,
                                           recordset['id'],
                                           Record(**record_values))

        elif event_type == 'network.floating_ip.disassociate':
            LOG.debug('FloatingV4Handler: Deleting A record for %s on %s', payload['floating_ip'], payload['instance_id'])

            self._delete(zone_id=domain_id,
                    resource_id=payload['instance_id'],
                    resource_type='instance')

            if reverse_id == None:
                LOG.debug('UNABLE TO DETERMINE REVERSE ZONE: %s', payload['floating_ip'])

            else:
                self._delete(zone_id=reverse_id,
                    resource_id=payload['instance_id'],
                    resource_type='instance')

