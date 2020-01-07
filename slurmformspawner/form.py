import re

from datetime import datetime
from subprocess import check_output

from jinja2 import Template
from traitlets import Integer, Unicode, Float
from wtforms import BooleanField, DecimalField, SelectField, StringField, Form, RadioField
from wtforms.validators import InputRequired
from wtforms.fields.html5 import IntegerField
from wtforms.widgets.html5 import NumberInput

def get_slurm_cpus():
    cpus = check_output(['sinfo', '-h', '-e', '--format=%c'], encoding='utf-8').split()
    return list(map(int, cpus))

def get_slurm_mems():
    mems = check_output(['sinfo', '-h', '-e', '--format=%m'], encoding='utf-8').split()
    return list(map(int, mems))

def get_slurm_accounts(username):
    output = check_output(['sacctmgr', 'show', 'user', username, 'withassoc',
                           'format=account', '-p', '--noheader'], encoding='utf-8').split()
    return [out.rstrip('|') for out in output]

def get_slurm_gres():
    return check_output(['sinfo', '-h', '--format=%G'], encoding='utf-8').split()

def get_slurm_active_reservations(username, accounts):
    accounts = set(accounts)
    reservations = check_output(['scontrol', 'show', 'res', '-o', '--quiet'], encoding='utf-8').strip().split('\n')
    if not reservations:
        return []
    reservations = [dict([item.split('=', maxsplit=1) for item in rsv.split()]) for rsv in reservations if rsv]
    for rsv in reservations:
        if rsv['State'] == 'ACTIVE':
            rsv['Users'] = set(rsv['Users'].split(','))
            rsv['Accounts'] = set(rsv['Accounts'].split(','))
            rsv['StartTime'] = datetime.strptime(rsv['StartTime'], "%Y-%m-%dT%H:%M:%S")
            rsv['EndTime'] = datetime.strptime(rsv['EndTime'], "%Y-%m-%dT%H:%M:%S")
            rsv['valid'] = username in rsv['Users'] or bool(accounts.intersection(rsv['Accounts']))
        else:
            rsv['valid'] = False
    return [rsv for rsv in reservations if rsv['valid']]

class SlurmSpawnerForm(Form):
    account = SelectField("Account")
    runtime = DecimalField('Time (hours)', validators=[InputRequired()], widget=NumberInput())
    gui     = RadioField('GUI',
                         choices=[('notebook', 'Jupyter Notebook'), ('lab', 'JupyterLab')],
                         default='notebook')
    nprocs  = IntegerField('Number of cores', validators=[InputRequired()], widget=NumberInput())
    memory  = IntegerField('Memory (MB)',  validators=[InputRequired()], widget=NumberInput())
    gpus    = SelectField('GPU configuration')
    oversubscribe = BooleanField('Enable core oversubscription?')
    reservation = SelectField("Reservation")

    def __init__(self, username, template_path, form_params, prev_values):
        super().__init__()
        self.username = username
        self.set_gpu_choices(get_slurm_gres())

        with open(template_path, 'r') as template_file:
            self.template = template_file.read()

        self.config_runtime(prev=prev_values.pop('runtime', None),
                            def_=form_params['runtime']['def_'],
                            min_=form_params['runtime']['min_'],
                            max_=form_params['runtime']['max_'],
                            step=form_params['runtime']['step'],
                            lock=form_params['runtime']['lock'])

        self.config_core(prev=prev_values.pop('nprocs', None),
                         def_=form_params['core']['def_'],
                         min_=form_params['core']['min_'],
                         max_=form_params['core']['max_'],
                         step=form_params['core']['step'],
                         lock=form_params['core']['lock'])

        self.config_memory(prev=prev_values.pop('memory', None),
                           def_=form_params['mem']['def_'],
                           min_=form_params['mem']['min_'],
                           max_=form_params['mem']['max_'],
                           step=form_params['mem']['step'],
                           lock=form_params['mem']['lock'])

        self.config_oversubscribe(prev=prev_values.pop('oversubscribe', None),
                                  def_=form_params['oversubscribe']['def_'],
                                  lock=form_params['oversubscribe']['lock'])

        for field, value in prev_values.items():
            if value:
                self[field].data = value

    def render(self):
        accounts = get_slurm_accounts(self.username)
        reservations = get_slurm_active_reservations(self.username, accounts)
        self.set_account_choices(accounts)
        self.set_reservations(reservations)
        return Template(self.template).render(form=self)

    def config_runtime(self, prev, def_, min_, max_, step, lock):
        if prev is not None and min_ <= prev <= max_:
            # time is converted in minutes after submitting
            self.runtime.data = round(prev / 60, 2)
        else:
            self.runtime.data = def_
        self.runtime.widget.min = min_
        if max_ > 0:
            self.runtime.widget.max = max_
        self.runtime.widget.step = step
        if lock:
            self.runtime.render_kw = {'disabled': 'disabled'}
        self.runtime.filters = [lambda x: int(x * 60)]

    def config_core(self, prev, def_, min_, max_, step, lock):
        if prev is not None and min_ <= prev <= max_:
            self.nprocs.data = prev
        else:
            self.nprocs.data = def_
        self.nprocs.widget.min = min_
        self.nprocs.widget.max = max(get_slurm_cpus())
        if max_ > 0:
            self.nprocs.widget.max = min(max_, self.nprocs.widget.max)
        self.nprocs.widget.step = step
        if lock:
            self.nprocs.render_kw = {'disabled': 'disabled'}

    def config_memory(self, prev, def_, min_, max_, step, lock):
        if prev is not None and min_ <= prev <= max_:
            self.memory.data = prev
        else:
            self.memory.data = def_
        self.memory.widget.min = min_
        self.memory.widget.max = max(get_slurm_mems())
        if max_ > 0:
            self.memory.widget.max = min(max_, self.memory.widget.max)
        self.memory.widget.step = step
        if lock:
            self.memory.render_kw = {'disabled': 'disabled'}

    def config_oversubscribe(self, prev, def_, lock):
        self.oversubscribe.data = def_
        if lock:
            self.oversubscribe.render_kw = {'disabled': 'disabled'}
        elif prev is not None:
            self.oversubscribe.data = prev

    def set_account_choices(self, accounts):
        self.account.choices = list(zip(accounts, accounts))

    def set_gpu_choices(self, gres_list):
        gpu_choices = {'gpu:0': 'None'}
        for gres in gres_list:
            match = re.match(r"(gpu:[\w:]+)", gres)
            if match:
                gres = match.group(1).split(':')
                number = int(gres[-1])
                if len(gres) == 2:
                    strings = ('gpu:{}', '{} x GPU')
                elif len(gres) > 2:
                    strings = ('gpu:{}:{{}}'.format(gres[1]), '{{}} x {}'.format(gres[1].upper()))
                for i in range(1, number + 1):
                    gpu_choices[strings[0].format(i)] = strings[1].format(i)
        self.gpus.choices = gpu_choices.items()

    def set_reservations(self, reservation_list):
        now = datetime.now()
        choices = [("", "None")]
        for rsv in reservation_list:
            name = rsv['ReservationName']
            duration = rsv['EndTime'] - now
            string = '{} - time left: {}'.format(name, duration)
            choices.append((name, string))
        self.reservation.choices = choices
