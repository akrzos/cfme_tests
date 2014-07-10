import pytest

from cfme.infrastructure import host
from cfme.fixtures import pytest_selenium as sel
from cfme.infrastructure.pxe import get_pxe_server_from_config, get_template_from_config
from cfme.services import requests
from cfme.web_ui import flash, fill
from cfme.infrastructure.provisioning import provisioning_form
from utils.conf import cfme_data
from utils.providers import setup_infrastructure_providers
from utils.log import logger
from utils.wait import wait_for
from utils import testgen

pytestmark = [
    pytest.mark.fixtureconf(server_roles="+automate +notifier"),
    pytest.mark.usefixtures('server_roles', 'uses_infra_providers')
]


def pytest_generate_tests(metafunc):
    # Filter out providers without host provisioning data defined
    argnames, argvalues, idlist = testgen.infra_providers(metafunc, 'host_provisioning')
    pargnames, pargvalues, pidlist = testgen.pxe_servers(metafunc)
    argnames = argnames + ['pxe_server', 'pxe_cust_template']
    pxe_server_names = [pval[0] for pval in pargvalues]

    new_idlist = []
    new_argvalues = []
    for i, argvalue_tuple in enumerate(argvalues):
        args = dict(zip(argnames, argvalue_tuple))
        if not args['host_provisioning']:
            # No host provisioning data available
            continue

        # required keys should be a subset of the dict keys set
        if not {'pxe_server', 'pxe_image', 'pxe_image_type', 'pxe_kickstart',
                'datacenter', 'cluster', 'datastores',
                'hostname', 'root_password', 'ip_addr',
                'subnet_mask', 'gateway', 'dns'}.issubset(args['host_provisioning'].viewkeys()):
            # Need all  for host provisioning
            continue

        pxe_server_name = args['host_provisioning']['pxe_server']
        if pxe_server_name not in pxe_server_names:
            continue

        pxe_cust_template = args['host_provisioning']['pxe_kickstart']
        if pxe_cust_template not in cfme_data['customization_templates'].keys():
            continue

        argvalues[i].append(get_pxe_server_from_config(pxe_server_name))
        argvalues[i].append(get_template_from_config(pxe_cust_template))
        new_idlist.append(idlist[i])
        new_argvalues.append(argvalues[i])

    testgen.parametrize(metafunc, argnames, new_argvalues, ids=new_idlist, scope="module")


@pytest.fixture(scope="module")
def setup_providers():
    # Normally function-scoped
    setup_infrastructure_providers()


@pytest.fixture(scope="module")
def setup_pxe_servers_host_prov(pxe_server, pxe_cust_template, host_provisioning):
    if not pxe_server.exists():
        pxe_server.create()
        pxe_server.set_pxe_image_type(host_provisioning['pxe_image'],
            host_provisioning['pxe_image_type'])
    if not pxe_cust_template.exists():
        pxe_cust_template.create()


@pytest.mark.usefixtures('setup_pxe_servers_host_prov')
def test_host_provisioning(setup_providers, cfme_data, host_provisioning, server_roles,
        provider_crud, smtp_test, request):

    # Add host before provisioning
    test_host = host.get_from_config('esx')
    test_host.create()

    # Populate provisioning_data before submitting host provisioning form
    pxe_server, pxe_image, pxe_image_type, pxe_kickstart, datacenter, cluster, datastores,\
        host_name, root_password, ip_addr, subnet_mask, gateway, dns = map(host_provisioning.get,
        ('pxe_server', 'pxe_image', 'pxe_image_type', 'pxe_kickstart', 'datacenter', 'cluster',
        'datastores', 'hostname', 'root_password', 'ip_addr', 'subnet_mask', 'gateway', 'dns'))

    def cleanup_host():
        try:
            logger.info('Cleaning up host %s on provider %s' % (host_name, provider_crud.key))
            mgmt_system = provider_crud.get_mgmt_system()
            host_list = mgmt_system.list_host()
            if host_provisioning['ip_addr'] in host_list.values():
                mgmt_system.remove_host_from_cluster(host_provisioning['ip_addr'])

            ipmi = test_host.get_ipmi()
            ipmi.power_off()

            # During host provisioning,the host name gets changed from what's specified at creation
            # time.If host provisioning succeeds,the original name is reverted to,otherwise the
            # changed names are retained upon failure
            renamed_host_name1 = "{} ({})".format('IPMI', host_provisioning['ipmi_address'])
            renamed_host_name2 = "{} ({})".format('VMware ESXi', host_provisioning['ip_addr'])

            host_list_ui = host.get_all_hosts()
            if host_provisioning['hostname'] in host_list_ui:
                test_host.delete(cancel=False)
                host.wait_for_host_delete(test_host)
            elif renamed_host_name1 in host_list_ui:
                host_renamed_obj1 = host.Host(name=renamed_host_name1)
                host_renamed_obj1.delete(cancel=False)
                host.wait_for_host_delete(host_renamed_obj1)
            elif renamed_host_name2 in host_list_ui:
                host_renamed_obj2 = host.Host(name=renamed_host_name2)
                host_renamed_obj2.delete(cancel=False)
                host.wait_for_host_delete(host_renamed_obj2)
        except:
            # The mgmt_sys classes raise Exception :\
            logger.warning('Failed to clean up host %s on provider %s' %
                (host_name, provider_crud.key))

    request.addfinalizer(cleanup_host)

    pytest.sel.force_navigate('infrastructure_provision_host', context={
        'host': test_host, })

    note = ('Provisioning host %s on provider %s' % (host_name, provider_crud.key))
    provisioning_data = {
        'email': 'template_provisioner@example.com',
        'first_name': 'Template',
        'last_name': 'Provisioner',
        'notes': note,
        'pxe_server': pxe_server,
        'pxe_image': {'name': [pxe_image]},
        'provider_name': provider_crud.name,
        'cluster': "{} / {}".format(datacenter, cluster),
        'datastore_name': {'name': datastores},
        'root_password': root_password,
        'host_name': host_name,
        'ip_address': ip_addr,
        'subnet_mask': subnet_mask,
        'gateway': gateway,
        'dns_servers': dns,
        'custom_template': {'name': [pxe_kickstart]},
    }

    fill(provisioning_form, provisioning_data, action=provisioning_form.host_submit_button)
    flash.assert_success_message(
        "Host Request was Submitted, you will be notified when your Hosts are ready")

    row_description = 'PXE install on [%s] from image [%s]' % (host_name, pxe_image)
    cells = {'Description': row_description}

    row, __ = wait_for(requests.wait_for_request, [cells],
        fail_func=requests.reload, num_sec=1500, delay=20)
    assert row.last_message.text == 'Host Provisioned Successfully'
    assert row.status.text != 'Error'

    # Navigate to host details page and verify Provider and cluster names
    sel.force_navigate('infrastructure_host', context={'host': test_host, })
    assert test_host.get_detail('Relationships', 'Infrastructure Provider') ==\
        provider_crud.name, 'Provider name does not match'

    assert test_host.get_detail('Relationships', 'Cluster') ==\
        host_provisioning['cluster'], 'Cluster does not match'

    # Navigate to host datastore page and verify that the requested datastore has been assigned
    # to the host
    requested_ds = host_provisioning['datastores']
    datastores = test_host.get_datastores()
    assert set(requested_ds).issubset(datastores), 'Datastores are missing some members'

    # Wait for e-mails to appear
    def verify():
        return (
            len(
                smtp_test.get_emails(
                    text_like="%%Your host request was approved%%"
                )
            ) > 0
            and len(
                smtp_test.get_emails(
                    subject_like="Your host provisioning request has Completed - Host:%%%s" %
                    host_name
                )
            ) > 0
        )

    wait_for(verify, message="email receive check", delay=5)