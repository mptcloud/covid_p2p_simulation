import pickle
import json
from base import Event
import subprocess
import numpy as np
from config import *
from utils import binary_to_float
import matplotlib.pyplot as plt
from utils import _encode_message, _decode_message
from bitarray import bitarray
import operator
from collections import defaultdict
import datetime
from tqdm import tqdm
from plots.plot_risk import dist_plot, hist_plot


""" This file contains the core of the side simulation, which is run on the output encounters from the main simulation.
It's primary functionality is to run the message clustering and risk prediction algorithms.
"""


# A utility class for re-inflating human objects with just the stuff we need for message passing / risk prediction
class dummy_human:
    def __init__(self, name=None, rng=None):
        self.name = name
        self.M = {}
        self.messages = []
        self.risk = 0
        self.rng = rng

    @property
    def message_risk(self):
        """quantizes the risk in order to be used in a message"""
        if self.risk == 1.0:
            return bitarray('1111')
        return bitarray(float_to_binary(self.risk, 0, 4))

    def cur_message(self, time):
        """creates the current message for this user"""
        Message = namedtuple('message', 'uid risk time unobs_id')
        message = Message(self.uid, self.message_risk, time, self.name)
        return message

    @property
    def uid(self):
        return self._uid

    def update_uid(self):
        try:
            self._uid.pop()
            self._uid.extend([self.rng.choice([True, False])])
        except AttributeError:
            self._uid = bitarray()
            self._uid.extend(self.rng.choice([True, False], 4))  # generate a random 4-bit code
    #
    # @property
    # def reported_symptoms(self):
    #     try:
    #         sickness_day = (self.env.timestamp - self.infection_timestamp).days
    #         return self.all_reported_symptoms[sickness_day]
    #     except Exception as e:
    #         return []



def risk_for_symptoms(human):
    """ This function calculates a risk score based on the person's symptoms."""
    # if they get tested, it takes TEST_DAYS to get the result, and they are quarantined for QUARANTINE_DAYS.
    # The test_timestamp is set to datetime.min, unless they get a positive test result.
    # Basically, once they know they have a positive test result, they have a risk of 1 until after quarantine days.
    if human.is_quarantined:
        return 1.

    symptoms = human.reported_symptoms_for_sickness()
    if 'severe' in symptoms:
        return 0.75
    if 'moderate' in symptoms:
        return 0.5
    if 'mild' in symptoms:
        return 0.25
    return 0.0


def add_message_to_cluster(human, m_i):
    """ This function clusters new messages by scoring them against old messages in a sort of naive nearest neighbors approach"""
    # TODO: include risk level in clustering, currently only uses quantized uid
    # TODO: refactor to compare multiple clustering schemes
    # TODO: check for mutually exclusive messages in order to break up a group and re-run nearest neighbors
    m_i_enc = _encode_message(m_i)
    m_risk = binary_to_float("".join([str(x) for x in np.array(m_i[1].tolist()).astype(int)]), 0, 4)

    # otherwise score against previous messages
    scores = {}
    for m_enc, _ in human.M.items():
        m = _decode_message(m_enc)
        if m_i[0] == m[0] and m_i[2].day == m[2].day:
            scores[m_enc] = 3
        elif m_i[0][:3] == m[0][:3] and m_i[2].day - 1 == m[2].day:
            scores[m_enc] = 2
        elif m_i[0][:2] == m[0][:2] and m_i[2].day - 2 == m[2].day:
            scores[m_enc] = 1
        elif m_i[0][:1] == m[0][:1] and m_i[2].day - 2 == m[2].day:
            scores[m_enc] = 0

    if scores:
        max_score_message = max(scores.items(), key=operator.itemgetter(1))[0]
        human.M[m_i_enc] = {'assignment': human.M[max_score_message]['assignment'], 'previous_risk': m_risk, 'carry_over_transmission_proba': RISK_TRANSMISSION_PROBA}
    # if it's either the first message
    elif len(human.M) == 0:
        human.M[m_i_enc] = {'assignment': 0, 'previous_risk': m_risk, 'carry_over_transmission_proba': RISK_TRANSMISSION_PROBA}
    # if there was no nearby neighbor
    else:
        new_group = max([v['assignment'] for k, v in human.M.items()]) + 1
        human.M[m_i_enc] = {'assignment': new_group, 'previous_risk': m_risk, 'carry_over_transmission_proba': RISK_TRANSMISSION_PROBA}


def update_risk_encounter(human, message, RISK_MODEL):
    """ This function updates an individual's risk based on the receipt of a new message"""

    # Get the binarized contact risk
    m_risk = binary_to_float("".join([str(x) for x in np.array(message[1].tolist()).astype(int)]), 0, 4)

    # select your contact risk prediction model
    update = 0
    if RISK_MODEL == 'yoshua':
        if human.risk < m_risk:
            update = (m_risk - m_risk * human.risk) * RISK_TRANSMISSION_PROBA
    elif RISK_MODEL == 'lenka':
        update = m_risk * RISK_TRANSMISSION_PROBA

    elif RISK_MODEL == 'eilif':
        msg_enc = _encode_message(message)
        if msg_enc not in human.M:
            # update is delta_risk
            update = m_risk * RISK_TRANSMISSION_PROBA
        else:
            previous_risk = human.M[msg_enc]['previous_risk']
            carry_over_transmission_proba = human.M[msg_enc]['carry_over_transmission_proba']
            update = ((m_risk - previous_risk) * RISK_TRANSMISSION_PROBA + previous_risk * carry_over_transmission_proba)

        # Update contact history
        human.M[msg_enc]['previous_risk'] = m_risk
        human.M[msg_enc]['carry_over_transmission_proba'] = RISK_TRANSMISSION_PROBA * (1 - update)

    human.risk += update

    # some models require us to clip the risk at one (like 'lenka' and 'eilif')
    if CLIP_RISK:
        human.risk = min(human.risk, 1.)

if __name__ == "__main__":
    # TODO: add as args that can be called from cmdline
    PLOT_DAILY = True
    PATH_TO_DATA = "output/data.pkl"
    PATH_TO_HUMANS = "output/humans.pkl"
    CLUSTER_PATH = "output/clusters.json"
    PATH_TO_PLOT = "plots/risk/"
    RISK_MODEL = 'yoshua'  # options: ['yoshua', 'lenka', 'eilif']
    METHOD_CLUSTERING_MAP = {"eilif": True, "yoshua": False, "lenka": False}
    LOGS_SUBSET_SIZE = 10000000
    seed = 0

    rng = np.random.RandomState(seed)

    # read and filter the pickles
    with open(PATH_TO_DATA, "rb") as f:
        logs = pickle.load(f)
    human_ids = set()
    enc_logs = []
    symp_logs = []
    for log in logs:
        human_ids.add(log['human_id'])
        if log['event_type'] == Event.encounter:
            enc_logs.append(log)
        elif log['event_type'] == Event.symptom_start or log['event_type'] == Event.recovered:
            print(log['payload'])
            symp_logs.append(log)
        else:
            print(log['event_type'])

    # Sort encounter logs by time and then by human id
    enc_logs = sorted(enc_logs, key=operator.itemgetter('time'))
    start = enc_logs[0]['time']
    logs = defaultdict(list)
    def hash_id_day(hid, day):
        return str(hid) + "-" + str(day)

    for log in enc_logs:
        day_since_epoch = (log['time'] - start).days
        logs[hash_id_day(log['human_id'], day_since_epoch)].append(log)

    import pdb; pdb.set_trace()
    symp_logs_proc = defaultdict(list)
    for log in symp_logs:
        symp_logs_proc['human_id'].append(log)
    # re-inflate the humans
    hd = {}
    for human_id in human_ids:
        hd[human_id] = dummy_human(name=human_id, rng=rng)

    risks = []
    days = (enc_logs[-1]['time'] - enc_logs[0]['time']).days
    for current_day in range(days):
        for hid, human in hd.items():
            human.update_uid()
            start_risk = human.risk

            # check if you have new reported symptoms
            human.risk = risk_for_symptoms(human)
            # read your old messages
            for m_i in human.messages:
                # update risk based on that day's messages
                if METHOD_CLUSTERING_MAP[RISK_MODEL]:
                    add_message_to_cluster(human, m_i)
                update_risk_encounter(human, m_i, RISK_MODEL)

            # go about your day and accrue encounters
            encounters = logs[hash_id_day(human.name, current_day)]
            for encounter in encounters:
                # extract variables from log
                encounter_time = encounter['time']
                unobs = encounter['payload']['unobserved']
                encountered_human = hd[unobs['human2']['human_id']]
                human.messages.append(encountered_human.cur_message(encounter_time))

            # TODO: if risk changed substantially, send update messages for all of my messages in a rolling 14 day window
            if start_risk > human.risk + 0.1 or start_risk < human.risk - 0.1:
                for m in human.messages:
                    if encounter_time - m.time < datetime.timedelta(days=14):
                        humans[m.unobs_id].messages.append(human.cur_message(encounter_time))

            # append the updated risk for this person and whether or not they are actually infectious
            risks.append((human.risk, human.is_infectious, human.is_exposed, human.name))
        print([(r[0], r[-1]) for r in risks])
        print(f"day: {current_day}")

        hist_plot(risks, f"{PATH_TO_PLOT}day_{str(current_day).zfill(3)}.png")
        risks = []


    # make a gif of the dist output
    process = subprocess.Popen(f"convert -delay 50 -loop 0 {PATH_TO_PLOT}/*.png {PATH_TO_PLOT}/risk.gif".split(), stdout=subprocess.PIPE)
    output, error = process.communicate()

    # write out the clusters to be processed by privacy_plots
    if METHOD_CLUSTERING_MAP[RISK_MODEL]:
        clusters = []
        for human in humans:
            clusters.append(human.M)
        json.dump(clusters, open(CLUSTER_PATH, 'w'))

