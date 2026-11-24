"""
Reintenta crear la máquina de Oracle (stack "Pelacord" de Resource Manager) hasta que haya lugar.

Cada corrida:
1. Si la máquina ya existe, se asegura de que tenga IP pública y termina con done=true.
2. Si hay un intento en curso, no hace nada.
3. Si no, lanza un "Apply" del stack, espera el resultado y, si salió bien, le pone IP pública.

Lee la configuración de variables de entorno (en GitHub Actions vienen de secrets/variables):
OCI_USER, OCI_FINGERPRINT, OCI_TENANCY, OCI_REGION, OCI_KEY (contenido de la llave privada) y,
opcional, OCI_STACK_ID y OCI_INSTANCE_NAME. En la PC, si no están, usa ~/.oci/config.
"""
import os
import sys
import time
from datetime import datetime, timezone

import oci

NAME = os.environ.get('OCI_INSTANCE_NAME', 'Pelacord')
ALIVE = {'PROVISIONING', 'STARTING', 'RUNNING', 'STOPPING', 'STOPPED'}


def config():
    if os.environ.get('OCI_KEY'):
        cfg = {
            'user': os.environ['OCI_USER'],
            'fingerprint': os.environ['OCI_FINGERPRINT'],
            'tenancy': os.environ['OCI_TENANCY'],
            'region': os.environ['OCI_REGION'],
            'key_content': os.environ['OCI_KEY'],
        }
    else:
        cfg = oci.config.from_file()
    oci.config.validate_config(cfg)
    return cfg


def output(**values):
    path = os.environ.get('GITHUB_OUTPUT')
    for key, value in values.items():
        print(f'{key}={value}')
        if path:
            with open(path, 'a', encoding='utf-8') as fh:
                fh.write(f'{key}={value}\n')


def find_instance(compute, tenancy):
    items = oci.pagination.list_call_get_all_results(compute.list_instances, tenancy, display_name=NAME).data
    alive = [i for i in items if i.lifecycle_state in ALIVE]
    return alive[0] if alive else None


def ensure_public_ip(cfg, compute, instance):
    """La instancia se crea sin IP pública: se le agrega una (efímera, gratis)."""
    network = oci.core.VirtualNetworkClient(cfg)
    for _ in range(30):
        instance = compute.get_instance(instance.id).data
        if instance.lifecycle_state == 'RUNNING':
            break
        time.sleep(10)
    attachments = compute.list_vnic_attachments(instance.compartment_id, instance_id=instance.id).data
    attached = [a for a in attachments if a.lifecycle_state == 'ATTACHED']
    if not attached:
        return None
    vnic = network.get_vnic(attached[0].vnic_id).data
    if vnic.public_ip:
        return vnic.public_ip
    private = network.list_private_ips(vnic_id=vnic.id).data
    primary = next((p for p in private if p.is_primary), private[0])
    created = network.create_public_ip(oci.core.models.CreatePublicIpDetails(
        compartment_id=instance.compartment_id, lifetime='EPHEMERAL', private_ip_id=primary.id,
    )).data
    return created.ip_address


def find_stack(rm, tenancy):
    stack_id = os.environ.get('OCI_STACK_ID')
    if stack_id:
        return stack_id
    stacks = rm.list_stacks(compartment_id=tenancy).data
    for stack in stacks:
        if stack.display_name.lower() == NAME.lower() and stack.lifecycle_state == 'ACTIVE':
            return stack.id
    sys.exit(f'No encontré el stack "{NAME}"')


def main():
    cfg = config()
    tenancy = cfg['tenancy']
    compute = oci.core.ComputeClient(cfg)
    rm = oci.resource_manager.ResourceManagerClient(cfg)

    instance = find_instance(compute, tenancy)
    if instance:
        ip = ensure_public_ip(cfg, compute, instance)
        print(f'La máquina ya existe ({instance.lifecycle_state}) y tiene IP pública: {bool(ip)}')
        output(done='true')
        return True

    stack_id = find_stack(rm, tenancy)
    jobs = rm.list_jobs(stack_id=stack_id, sort_by='TIMECREATED', sort_order='DESC', limit=1).data
    if jobs and jobs[0].lifecycle_state in ('ACCEPTED', 'IN_PROGRESS'):
        print('Hay un intento en curso; espero al próximo turno.')
        return False

    job = rm.create_job(oci.resource_manager.models.CreateJobDetails(
        stack_id=stack_id,
        display_name=f'auto-retry-{int(time.time())}',
        job_operation_details=oci.resource_manager.models.CreateApplyJobOperationDetails(execution_plan_strategy='AUTO_APPROVED'),
    )).data
    deadline = time.time() + 240
    state = job.lifecycle_state
    while time.time() < deadline:
        time.sleep(15)
        state = rm.get_job(job.id).data.lifecycle_state
        if state in ('SUCCEEDED', 'FAILED', 'CANCELED'):
            break
    stamp = datetime.now(timezone.utc).strftime('%H:%M UTC')
    print(f'[{stamp}] Resultado: {state}' + (' (sin lugar, sigo probando)' if state == "FAILED" else ''))
    if state == 'SUCCEEDED':
        instance = find_instance(compute, tenancy)
        ip = ensure_public_ip(cfg, compute, instance) if instance else None
        print('¡Máquina creada! Tiene IP pública:', bool(ip))
        output(done='true')
        return True
    return False


def loop(minutes, every):
    """Prueba cada `every` segundos durante `minutes` minutos (o hasta que salga)."""
    end = time.time() + minutes * 60
    while True:
        try:
            if main():
                return
        except Exception as error:  # un error de red no corta el ciclo
            print('Error en el intento:', type(error).__name__, str(error)[:200])
        if time.time() + every > end:
            print('Se terminó el tiempo de esta corrida; la próxima sigue.')
            output(done='false')
            return
        time.sleep(every)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'loop':
        loop(float(sys.argv[2]) if len(sys.argv) > 2 else 330, float(sys.argv[3]) if len(sys.argv) > 3 else 210)
    else:
        main()
