#!/usr/bin/env python

# This is free and unencumbered software released into the public domain.
#
# Anyone is free to copy, modify, publish, use, compile, sell, or
# distribute this software, either in source code form or as a compiled
# binary, for any purpose, commercial or non-commercial, and by any
# means.
#
# In jurisdictions that recognize copyright laws, the author or authors
# of this software dedicate any and all copyright interest in the
# software to the public domain. We make this dedication for the benefit
# of the public at large and to the detriment of our heirs and
# successors. We intend this dedication to be an overt act of
# relinquishment in perpetuity of all present and future rights to this
# software under copyright law.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
# ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# For more information, please refer to <http://unlicense.org/>

import boto.ec2
import boto.route53
import time
import yaml
import os.path
import sys
import argparse
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


class EC2Launcher(object):

  def __init__(self, settings, config, base):
    self.settings = settings
    self.config = self._merge_config(base, config)
    self.config = self._merge_config(settings.get('profileDefaults'), config)
  
  def _merge_config(self, defaults, config):
    if isinstance(config,dict) and isinstance(defaults,dict):
      for k,v in defaults.iteritems():
        if k not in config:
          config[k] = v
        else:
          config[k] = self._merge_config(config[k],v)
  
    return config


  def _add_dns_record(self, hosted_zone, record_name, record_type, record_value, record_ttl=300, record_comment=''):
    from boto.route53.record import ResourceRecordSets
    conn = boto.connect_route53()

    zone = conn.get_zone(hosted_zone)
    changes = ResourceRecordSets(conn, zone.id, record_comment)
    change = changes.add_change('CREATE', '%s.%s' % (record_name, hosted_zone), record_type, record_ttl)
    change.add_value(record_value)
    changes.commit()


  def _get_user_data(self):
    cloud_config =  self.settings['cloud_config'] \
                   .replace('$HOSTNAME', self.config['hostname']) \
                   .replace('$PUBLIC_DOMAIN', self.config['route53']['publicDomain']) \
                   .replace('$PRIVATE_DOMAIN', self.config['route53']['privateDomain'])

    combined = MIMEMultipart()
    cloud_config = MIMEText(cloud_config, _subtype='cloud-config')
    cloud_config.add_header('Content-Disposition', 'attachment', filename='cloud-config.txt')
    combined.attach(cloud_config)
    user_script = MIMEText(self.settings['user_script'], _subtype='x-shellscript')
    user_script.add_header('Content-Disposition', 'attachment', filename='user-script.txt')
    combined.attach(user_script)

    return combined.as_string()


  def launch_instance(self):
    conn_ec2 = boto.ec2.connect_to_region(self.config['ec2']['availabilityZone'])

    bdm = boto.ec2.blockdevicemapping.BlockDeviceMapping()
    dev_sda1 = boto.ec2.blockdevicemapping.EBSBlockDeviceType()
    dev_sda1.size = self.config['ec2']['rootVolumeSize']
    dev_sda1.delete_on_termination = False
    dev_sda1.volume_type = 'gp2'
    bdm['/dev/sda1'] = dev_sda1
    
    eni = conn_ec2.create_network_interface(self.config['ec2']['subnet'],
                                            groups=config['ec2']['securityGroups'])
    
    if self.config['public']:
      eip = conn_ec2.allocate_address(domain='vpc')
      aa = conn_ec2.associate_address(allocation_id=eip.allocation_id,
                                      network_interface_id=eni.id)

    dev_eth0 = boto.ec2.networkinterface.NetworkInterfaceSpecification()
    dev_eth0.network_interface_id = eni.id
    dev_eth0.device_index = 0

    interfaces = boto.ec2.networkinterface.NetworkInterfaceCollection(dev_eth0)

    reservation = conn_ec2.run_instances(
      self.config['ec2']['ami'],
      key_name = self.config['ec2']['key'],
      instance_type = self.config['ec2']['instanceType'],
      user_data = self._get_user_data(),
      monitoring_enabled = False,
      disable_api_termination = True,
      instance_initiated_shutdown_behavior = 'stop',
      tenancy = 'default',
      ebs_optimized = self.config['ec2']['ebsOptimized'],
      network_interfaces = interfaces,
      block_device_map = bdm)

    instance = reservation.instances[0]


    return (instance, eip)


  def run(self):

    print('Waiting for instance to start...')

    instance = self.launch_instance()

    for i in range(0,10):
      while True:
        try:
          status = instance[0].update()
          while status != 'running':
            time.sleep(5)

            status = instance[0].update()
        except boto.exception.EC2ResponseError:
          time.sleep(5)
          continue
        break

    if status == 'running':

      # route53
      self._add_dns_record(self.config['route53']['privateDomain'], self.config['hostname'], 'A', instance[0].private_ip_address)
      if self.config['public']:
        self._add_dns_record(self.config['route53']['publicDomain'], self.config['hostname'], 'A', instance[1].public_ip)

      # write out hiera yaml
      hiera_data = dict(
        roles = self.config['puppetRoles']
      )

      with open(os.path.join(self.settings['hieraHostPath'],
                self.config['hostname'] + '.' + self.config['route53']['privateDomain'] + '.yaml'), 'w') as hiera_file:
        hiera_file.write(yaml.dump(hiera_data, default_flow_style=False))
        hiera_file.write(self.config['hieraData'])

      # name it
      instance[0].add_tag('Name', self.config['hostname'])

      print('New instance "' + instance[0].id + '" accessible at ' + instance[0].public_dns_name)
      return True
    else:
      return False
      print('Instance status: ' + status)



def parse_args():
  base_parser = argparse.ArgumentParser(description='Provisions new EC2 instance.')

  server_opts = { }
  base_parser.add_argument('config_file',
                           help='CONFIG_FILE',
                           **server_opts)

  base_parser.add_argument('--base',
                           help='Base YAML template to use.',
                           action='store',
                           default=False,
                           dest='base_file')

  return base_parser.parse_args()


if __name__ == '__main__':
  args = parse_args()
  settings = yaml.load(open('settings.yaml', 'r'))
  
  base = yaml.load(open(args.base_file, 'r')) if args.base_file else None

  config = yaml.load(open(args.config_file, 'r'))
 
  ec2_launcher = EC2Launcher(settings, config, base)
  ec2_launcher.run()
