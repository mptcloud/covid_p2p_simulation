"""
Contains base classes that define environment of the simulator.
"""
import numpy as np
import copy
import datetime
import itertools
import logging
import math
import time
from collections import defaultdict, Counter

import simpy

from covid19sim.utils import compute_distance, _get_random_area, relativefreq2absolutefreq, \
    get_test_false_negative_rate, calculate_average_infectiousness, get_rec_level_transition_matrix
from covid19sim.track import Tracker
from covid19sim.interventions import *
from covid19sim.frozen.message_utils import UIDType, UpdateMessage, combine_update_messages, \
    RealUserIDType

if typing.TYPE_CHECKING:
    from covid19sim.simulator import Human

PersonalMailboxType = typing.Dict[UIDType, typing.List[UpdateMessage]]
SimulatorMailboxType = typing.Dict[RealUserIDType, PersonalMailboxType]

from unittest.mock import Mock
logging = Mock()


class Env(simpy.Environment):
    """
    Custom simpy.Environment
    """

    def __init__(self, initial_timestamp):
        """
        Args:
            initial_timestamp (datetime.datetime): The environment's initial timestamp
        """
        self.initial_timestamp = datetime.datetime.combine(initial_timestamp.date(),
                                                           datetime.time())
        self.ts_initial = int(self.initial_timestamp.timestamp())
        super().__init__(self.ts_initial)

    @property
    def timestamp(self):
        """
        Returns:
            datetime.datetime: Current date.
        """
        #
        ##
        ## The following is preferable, but not equivalent to the initial
        ## version, because timedelta ignores Daylight Saving Time.
        ##
        #
        #return datetime.datetime.fromtimestamp(int(self.now))
        #
        return self.initial_timestamp + datetime.timedelta(
            seconds=self.now-self.ts_initial)

    def minutes(self):
        """
        Returns:
            int: Current timestamp minute
        """
        return self.timestamp.minute

    def hour_of_day(self):
        """
        Returns:
            int: Current timestamp hour
        """
        return self.timestamp.hour

    def day_of_week(self):
        """
        Returns:
            int: Current timestamp day of the week
        """
        return self.timestamp.weekday()

    def is_weekend(self):
        """
        Returns:
            bool: Current timestamp day is a weekend day
        """
        return self.day_of_week() >= 5

    def time_of_day(self):
        """
        Time of day in iso format
        datetime(2020, 2, 28, 0, 0) => '2020-02-28T00:00:00'

        Returns:
            str: iso string representing current timestamp
        """
        return self.timestamp.isoformat()


class City:
    """
    City
    """

    def __init__(self, env, n_people, init_percent_sick, rng, x_range, y_range, human_type, conf):
        """
        Args:
            env (simpy.Environment): [description]
            n_people (int): Number of people in the city
            init_percent_sick: % of population to be infected on day 0
            rng (np.random.RandomState): Random number generator
            x_range (tuple): (min_x, max_x)
            y_range (tuple): (min_y, max_y)
            human_type (covid19.simulator.Human): Class for the city's human instances
            conf (dict): yaml configuration of the experiment
        """
        self.conf = conf
        self.env = env
        self.rng = np.random.RandomState(rng.randint(2 ** 16))
        self.x_range = x_range
        self.y_range = y_range
        self.total_area = (x_range[1] - x_range[0]) * (y_range[1] - y_range[0])
        self.n_people = n_people
        self.init_percent_sick = init_percent_sick
        self.hash = int(time.time_ns())  # real-life time used as hash for inference server data hashing
        self.tracker = Tracker(env, self)

        self.test_type_preference = list(zip(*sorted(conf.get("TEST_TYPES").items(), key=lambda x:x[1]['preference'])))[0]
        self.max_capacity_per_test_type = {
            test_type: max([int(conf['TEST_TYPES'][test_type]['capacity'] * self.n_people), 1])
            for test_type in self.test_type_preference
        }

        if 'DAILY_TARGET_REC_LEVEL_DIST' in conf:
            # QKFIX: There are 4 recommendation levels, value is hard-coded here
            self.daily_target_rec_level_dists = (np.asarray(conf['DAILY_TARGET_REC_LEVEL_DIST'], dtype=np.float_)
                                                   .reshape((-1, 4)))
        else:
            self.daily_target_rec_level_dists = None
        self.daily_rec_level_mapping = None
        self.covid_testing_facility = TestFacility(self.test_type_preference, self.max_capacity_per_test_type, env, conf)

        print("Initializing locations ...")
        self.initialize_locations()

        self.humans = []
        self.hd = {}  # previously a cached property for some unknown reason
        self.households = OrderedSet()
        print("Initializing humans ...")
        self.initialize_humans(human_type)
        for human in self.humans:
            human.track_this_guy = True
            if human.is_exposed:
                print(human, human.household, human.household.residents)

        self.log_static_info()

        print("Computing their preferences")
        self._compute_preferences()
        self.intervention = None

        # GAEN summary statistics that enable the individual to determine whether they should send their info
        self.risk_change_histogram = Counter()
        self.risk_change_histogram_sum = 0
        self.sent_messages_by_day: typing.Dict[int, int] = {}

        # note: for good efficiency in the simulator, we will not allow humans to 'download'
        # database diffs between their last timeslot and their current timeslot; instead, we
        # will give them the global mailbox object (a dictionary) and have them 'pop' all
        # messages they consume from their own (simulation-only!) personal mailbox
        self.global_mailbox: SimulatorMailboxType = defaultdict(dict)
        self.tracker.initialize()

    def cleanup_global_mailbox(
            self,
            current_timestamp: datetime.datetime,
    ):
        """Removes all messages older than 14 days from the global mailbox."""
        # note that to keep the simulator efficient, users will directly pop the update messages that
        # they consume, so this is only necessary for edge cases (e.g. dead people can't update)
        max_encounter_age = self.conf.get('TRACING_N_DAYS_HISTORY')
        new_global_mailbox = defaultdict(dict)
        for user_key, personal_mailbox in self.global_mailbox.items():
            new_personal_mailbox = {}
            for mailbox_key, messages in personal_mailbox.items():
                for message in messages:
                    if (current_timestamp - message.encounter_time).days <= max_encounter_age:
                        if mailbox_key not in new_personal_mailbox:
                            new_personal_mailbox[mailbox_key] = []
                        new_personal_mailbox[mailbox_key].append(message)
            new_global_mailbox[user_key] = new_personal_mailbox
        self.global_mailbox = new_global_mailbox

    def register_new_messages(
            self,
            current_day_idx: int,
            current_timestamp: datetime.datetime,
            update_messages: typing.List[UpdateMessage],
            prev_human_risk_history_maps: typing.Dict["Human", typing.Dict[int, float]],
            new_human_risk_history_maps: typing.Dict["Human", typing.Dict[int, float]],
    ):
        """Adds new update messages to the global mailbox, passing them to trackers if needed.

        This function may choose to discard messages based on the mailbox server protocol being used.
        For this purpose, the risk history level maps of all users are provided as input. Note that
        these risk history maps may not actually be transfered to the server, and we have to be
        careful how we use this data in a safe fashion so that (in real life) only non-PII info
        needs to be transmitted to the server for filtering.
        """
        humans_with_updates = {self.hd[m._sender_uid] for m in update_messages}

        if self.conf.get("USE_GAEN"):
            # update risk level change histogram using scores of new updaters
            updater_scores = {
                h: h.contact_book.get_risk_level_change_score(
                    prev_risk_history_map=prev_human_risk_history_maps[h],
                    curr_risk_history_map=new_human_risk_history_maps[h],
                    proba_to_risk_level_map=h.proba_to_risk_level_map,
                ) for h in humans_with_updates
            }
            for score in updater_scores.values():
                self.risk_change_histogram[score] += 1
            self.risk_change_histogram_sum += len(updater_scores)
        else:
            # TODO: add filtering steps (if any) for other protocols
            pass

        # note that to keep the simulator efficient, users will have their own private mailbox inside
        # the global mailbox (this replaces the database diff logic & allows faster access)
        for update_message in update_messages:
            source_human = self.hd[update_message._sender_uid]
            destination_human = self.hd[update_message._receiver_uid]

            if self.conf.get("USE_GAEN"):
                should_send = self._check_should_send_message_gaen(
                    current_day_idx=current_day_idx,
                    current_timestamp=current_timestamp,
                    human=source_human,
                    risk_change_score=updater_scores[source_human],
                )
                if not should_send:
                    continue
            else:
                # TODO: add filtering steps (if any) for other protocols
                pass

            # if we get here, it's because we actually chose to send the message
            self.tracker.track_update_messages(
                from_human=source_human,
                to_human=destination_human,
                payload={
                    "reason": update_message._update_reason,
                    "new_risk_level": update_message.new_risk_level
                },
            )
            if current_day_idx not in self.sent_messages_by_day:
                self.sent_messages_by_day[current_day_idx] = 0
            self.sent_messages_by_day[current_day_idx] += 1
            source_human.contact_book.latest_update_time = \
                max(source_human.contact_book.latest_update_time, current_timestamp)
            mailbox_key = update_message.uid  # mailbox key = message uid
            personal_mailbox = self.global_mailbox[destination_human.name]
            if mailbox_key not in personal_mailbox:
                personal_mailbox[mailbox_key] = []
            personal_mailbox[mailbox_key].append(update_message)

    def _check_should_send_message_gaen(
            self,
            current_day_idx: int,
            current_timestamp: datetime.datetime,
            human: "Human",
            risk_change_score: int,
    ) -> bool:
        """Returns whether a specific human with a given GAEN impact score should send updates or not."""
        message_budget = self.conf.get("MESSAGE_BUDGET_GAEN")
        # give at least BURN_IN_DAYS days to get risk histograms
        if current_day_idx - self.conf.get("INTERVENTION_DAY") < self.conf.get("BURN_IN_DAYS"):
            return False
        days_between_messages = self.conf.get("DAYS_BETWEEN_MESSAGES")
        # don't send messages if you sent recently
        if (current_timestamp - human.contact_book.latest_update_time).days < days_between_messages:
            return False
        # don't exceed the message budget
        already_sent_messages = self.sent_messages_by_day.get(current_day_idx, 0)
        if already_sent_messages >= message_budget * self.conf.get("n_people"):
            return False

        # if people uniformly send messages in the population, then 1 / days_between_messages people
        # won't send messages today anyway
        # FIXME this overestimates messages but it's ok (??) because we have a hard daily cap
        message_budget = days_between_messages * message_budget
        # but if we don't have that we underestimate the number of available messages in the budget
        # because some people will have already sent a message the day before and won't be able to
        # on this day

        # reduce due to multiple updates per day so uniformly distribute messages
        message_budget = message_budget / self.conf.get("UPDATES_PER_DAY")

        # sorted list (decreasing order) of tuples (risk_change, proportion of people with that risk_change)
        scaled_risks = sorted(
            {
                k: v / self.risk_change_histogram_sum
                for k, v in self.risk_change_histogram.items()
            }.items(),
            reverse=True
        )
        summed_percentiles = 0
        for rc, percentile in scaled_risks:
            # there's room for this person's message
            if risk_change_score == rc and summed_percentiles < message_budget:
                # coin flip if you're in this bucket but this is the "last" bucket
                if summed_percentiles + percentile > message_budget:
                    # Out of all the available messages (MAX_NUM_MESSAGES_GAEN), summed_percentile
                    # have been sent. There remains (MAX - summed) messages to split across percentile people
                    if self.rng.random() < (message_budget - summed_percentiles):  # / percentile:
                        return True
                # otherwise there is room for messages so you should send
                else:
                    return True
            summed_percentiles += percentile
        return False

    @property
    def start_time(self):
        return datetime.datetime.fromtimestamp(self.env.ts_initial)

    def create_location(self, specs, type, name, area=None):
        """
        Create a location instance based on `type`

        Specs is a dict like:
        {
            "n" : (int) number of such locations,
            "area": (float) locations' typical area,
            "social_contact_factor": (float(0:1)) how much people are close to each other
                see contamination_probability(),
            "surface_prob": [0.1, 0.1, 0.3, 0.2, 0.3], distribution over types of surfaces
                in that location
            "rnd_capacity": (tuple, optional) Either None or a tuple of ints (min, max)
            describing the args of np.random.randint,
        }

        Args:
            specs (dict): location's parameters
            type (str): "household" and "senior_residency" create a Household instance,
                "hospital" creates a Hospital, other strings create a generic Location
            name (str): Location's name, created as `type:name`
            area (float, optional): Location's area. Defaults to None.

        Returns:
            Location | Household | Hospital: new location instance
        """
        _cls = Location
        if type in ['household', 'senior_residency']:
            _cls = Household
        if type == 'hospital':
            _cls = Hospital

        return   _cls(
                        env=self.env,
                        rng=self.rng,
                        name=f"{type}:{name}",
                        location_type=type,
                        lat=self.rng.randint(*self.x_range),
                        lon=self.rng.randint(*self.y_range),
                        area=area,
                        social_contact_factor=specs['social_contact_factor'],
                        capacity=None if not specs['rnd_capacity'] else self.rng.randint(*specs['rnd_capacity']),
                        surface_prob = specs['surface_prob']
                        )

    def initialize_locations(self):
        """
        Create locations according to config.py / LOCATION_DISTRIBUTION.
        The City instance will have attributes <location_type>s = list(location(*args))
        """
        for location, specs in self.conf.get("LOCATION_DISTRIBUTION").items():
            # household distribution is separate
            if location in ['household']:
                continue

            n = math.ceil(self.n_people/specs["n"])
            area = _get_random_area(n, specs['area'] * self.total_area, self.rng)
            locs = [self.create_location(specs, location, i, area[i]) for i in range(n)]
            setattr(self, f"{location}s", locs)

    def initialize_humans(self, human_type):
        """
        `Human`s are created based on the statistics captured in HUMAN_DSITRIBUTION. Age distribution is specified via 'p'.
        SMARTPHONE_OWNER_FRACTION_BY_AGE defines the fraction of population in the age bin that owns smartphone.
        APP_UPTAKE is the percentage of people in an age-bin who will adopt an app. (source: refer comments in core.yaml)

        Profession of `Human` is determined via `profession_profile` in each age bin. (no-source)

        Allocate humans to houses such that (unsolved)
            1. average number of residents in a house is (approx.) 2.5 (enforced via HOUSE_SIZE_PREFERENCE in core.yaml)
            2. not all residents are below MIN_AVG_HOUSE_AGE years of age
            3. age occupancy distribution follows HUMAN_DSITRIBUTION.residence_preference.house_size (no-source)

        current implementation is an approximate heuristic

        Args:
            human_type (Class): Class for the city's human instances
        """
        # make humans
        count_humans = 0
        house_allocations = {2:[], 3:[], 4:[], 5:[]}
        n_houses = 0

        # initial infections
        init_infected = math.ceil(self.init_percent_sick * self.n_people)
        chosen_infected = set(self.rng.choice(self.n_people, init_infected, replace=False).tolist())

        self.age_histogram = relativefreq2absolutefreq(
            bins_fractions={age_bin: specs['p'] for age_bin, specs in self.conf.get('HUMAN_DISTRIBUTION').items()},
            n_elements=self.n_people,
            rng=self.rng
        )

        for age_bin, specs in self.conf.get("HUMAN_DISTRIBUTION").items():
            n = self.age_histogram[age_bin]
            ages = ages = self.rng.randint(low=age_bin[0], high=age_bin[1]+1, size=n) # high is exclusive

            senior_residency_preference = specs['residence_preference']['senior_residency']

            # draw professions randomly for everyone
            professions = ['healthcare', 'school', 'others', 'retired']
            p = [specs['profession_profile'][x] for x in professions]
            profession = self.rng.choice(professions, p=p, size=n)

            for i in range(n):
                count_humans += 1
                age = ages[i]

                # select if residence is one of senior residency
                res = None
                if self.rng.random() < senior_residency_preference:
                    res = self.rng.choice(self.senior_residencys)

                # workplace
                if profession[i] == "healthcare":
                    workplace = self.rng.choice(self.hospitals + self.senior_residencys)
                elif profession[i] == 'school':
                    workplace = self.rng.choice(self.schools)
                elif profession[i] == 'others':
                    type_of_workplace = self.rng.choice(
                        [0,1,2],
                        p=self.conf.get("OTHERS_WORKPLACE_CHOICE"),
                        size=1
                    ).item()
                    type_of_workplace = [self.workplaces, self.stores, self.miscs][type_of_workplace]
                    workplace = self.rng.choice(type_of_workplace)
                else:
                    workplace = res

                self.humans.append(human_type(
                        env=self.env,
                        city=self,
                        rng=self.rng,
                        has_app=False,      # has_app gets set later when we intervene
                        name=count_humans,
                        age=age,
                        household=res,
                        workplace=workplace,
                        profession=profession[i],
                        rho=self.conf.get("RHO"),
                        gamma=self.conf.get("GAMMA"),
                        infection_timestamp=self.start_time if count_humans - 1 in chosen_infected else None,
                        conf=self.conf
                        )
                    )

        # assign houses; above only assigns senior residence
        # stores tuples - (location, current number of residents, maximum number of residents allowed)
        remaining_houses = []
        # for cases when human is below certain age and can't live in a single house
        house_size_preference = copy.deepcopy(self.conf.get('HOUSE_SIZE_PREFERENCE'))
        house_size_preference[0] = 0.0
        house_size_preference = np.array(house_size_preference)
        renormalized_house_size_preference = house_size_preference / house_size_preference.sum()

        permuted_humans = [self.humans[x] for x in self.rng.permutation(len(self.humans))]
        for human in permuted_humans:
            if human.household is not None:
                continue

            # if all of the available houses are at a capacity, create a new one with predetermined capacity
            if len(remaining_houses) == 0:
                cap = self.rng.choice(range(1,6), p=self.conf.get("HOUSE_SIZE_PREFERENCE"), size=1)
                x = self.create_location(
                    self.conf.get("LOCATION_DISTRIBUTION")['household'],
                    'household',
                    len(self.households)
                )

                remaining_houses.append((x, cap))

            # if there are houses that are ready to accommodate human, find a best match
            res = None
            for  c, (house, n_vacancy) in enumerate(remaining_houses):
                new_avg_age = (human.age + sum(x.age for x in house.residents))/(len(house.residents) + 1)
                if new_avg_age > self.conf.get("MIN_AVG_HOUSE_AGE"):
                    res = house
                    n_vacancy -= 1
                    if n_vacancy == 0:
                        remaining_houses = remaining_houses[:c] + remaining_houses[c+1:]
                    break

            # if there was no best match because of the check on MIN_AVG_HOUSE_AGE, allocate a new house based on
            # residence_preference of that age bin
            if res is None:
                for i, (l,u) in enumerate(self.conf.get("HUMAN_DISTRIBUTION").keys()):
                    if l <= human.age <= u:
                        bin = (l,u)
                        break

                if human.age <= self.conf.get("MIN_AVG_HOUSE_AGE"):
                    house_size_preference = renormalized_house_size_preference

                cap = self.rng.choice(range(1,6), p=house_size_preference, size=1)
                res = self.create_location(
                    self.conf.get("LOCATION_DISTRIBUTION")['household'],
                    'household',
                    len(self.households)
                )
                if cap - 1 > 0:
                    remaining_houses.append((res, cap-1))

            # FIXME: there is some circular reference here
            res.residents.append(human)
            human.assign_household(res)
            self.households.add(res)

        # assign area to house
        area = _get_random_area(
            len(self.households),
            self.conf.get("LOCATION_DISTRIBUTION")['household']['area'] * self.total_area,
            self.rng
        )

        for i,house in enumerate(self.households):
            house.area = area[i]

        # we don't need a cached property, this will do fine
        self.hd = {human.name: human for human in self.humans}

    def have_some_humans_download_the_app(self):
        """
        This method is called on intervention day if the intervention type is
        `Tracing`. It simulates the process of downloading the app on for smartphone
        users according to `APP_UPTAKE` and `SMARTPHONE_OWNER_FRACTION_BY_AGE`.
        """
        print("Downloading the app...")
        age_histogram = relativefreq2absolutefreq(
            bins_fractions={age_bin: specs['p']
                            for age_bin, specs
                            in self.conf.get('HUMAN_DISTRIBUTION').items()},
            n_elements=self.n_people,
            rng=self.rng
        )
        # app users
        all_has_app = self.conf.get('APP_UPTAKE') < 0
        # The dict below keeps track of an app quota for each age group
        n_apps_per_age = {
            k: math.ceil(self.age_histogram[k] * v * self.conf.get('APP_UPTAKE'))
            for k, v in self.conf.get("SMARTPHONE_OWNER_FRACTION_BY_AGE").items()
        }
        for human in self.humans:
            if all_has_app:
                # i get an app, you get an app, everyone gets an app
                human.has_app = True
                continue

            # Find what age bin the human is in
            age_bin = None
            for x in n_apps_per_age:
                if x[0] <= human.age <= x[1]:
                    age_bin = x
                    break
            assert age_bin is not None

            if n_apps_per_age[age_bin] > 0:
                # This human gets an app If there's quota left in his age group
                human.has_app = True
                n_apps_per_age[age_bin] -= 1
                continue

        self.tracker.adoption_rate = sum(h.has_app for h in self.humans) / len(self.humans)
        print(f"adoption rate: {100*self.tracker.adoption_rate:3.2f}%")

    def add_to_test_queue(self, human):
        self.covid_testing_facility.add_to_test_queue(human)

    def log_static_info(self):
        """
        Logs events for all humans in the city
        """
        for h in self.humans:
            Event.log_static_info(self.conf['COLLECT_LOGS'], self, h, self.env.timestamp)

    @property
    def events(self):
        """
        Get all events of all humans in the city

        Returns:
            list: all of everyone's events
        """
        return list(itertools.chain(*[h.events for h in self.humans]))

    def compute_daily_rec_level_distribution(self):
        # QKFIX: There are 4 recommendation levels, the value is hard-coded here
        counts = np.zeros((4,), dtype=np.float_)
        for human in self.humans:
            if human.has_app:
                counts[human.rec_level] += 1

        if np.sum(counts) > 0:
            counts /= np.sum(counts)

        return counts

    def compute_daily_rec_level_mapping(self, current_day):
        # If there is no target recommendation level distribution
        if self.daily_target_rec_level_dists is None:
            return None

        if self.conf.get('INTERVENTION_DAY', -1) < 0:
            return None
        else:
            current_day -= self.conf.get('INTERVENTION_DAY')
            if current_day < 0:
                return None

        daily_rec_level_dist = self.compute_daily_rec_level_distribution()

        index = min(current_day, len(self.daily_target_rec_level_dists) - 1)
        daily_target_rec_level_dist = self.daily_target_rec_level_dists[index]

        return get_rec_level_transition_matrix(daily_rec_level_dist,
                                               daily_target_rec_level_dist)

    def events_slice(self, begin, end):
        """
        Get all sliced events of all humans in the city

        Args:
            begin (datetime.datetime): minimum time of events
            end (int): maximum time of events

        Returns:
            list: The list each human's events, restricted to a slice
        """
        return list(itertools.chain(*[h.events_slice(begin, end) for h in self.humans]))

    def pull_events_slice(self, end):
        """
        Get the list of all human's events before `end`.
        /!\ Modifies each human's events

        Args:
            end (datetime.datetime): maximum time of pulled events

        Returns:
            list: All the events which occured before `end`
        """
        return list(itertools.chain(*[h.pull_events_slice(end) for h in self.humans]))

    def _compute_preferences(self):
        """
        Compute preferred distribution of each human for park, stores, etc.
        /!\ Modifies each human's stores_preferences and parks_preferences
        """
        for h in self.humans:
            h.stores_preferences = [(compute_distance(h.household, s) + 1e-1) ** -1 for s in self.stores]
            h.parks_preferences = [(compute_distance(h.household, s) + 1e-1) ** -1 for s in self.parks]

    def run(self, duration, outfile):
        """
        Run the City.
        Several daily tasks take place here.
        Examples include - (1) modifying behavior of humans based on an intervention
        (2) gathering daily statistics on humans

        Args:
            duration (int): duration of a step, in seconds.
            outfile (str): may be None, the run's output file to write to

        Yields:
            simpy.Timeout
        """
        humans_notified = False
        tmp_M = self.conf.get("GLOBAL_MOBILITY_SCALING_FACTOR")
        self.conf["GLOBAL_MOBILITY_SCALING_FACTOR"] = 1
        last_day_idx = 0
        while True:
            current_day = (self.env.timestamp - self.start_time).days
            # Notify humans to follow interventions on intervention day
            if current_day == self.conf.get('INTERVENTION_DAY') and not humans_notified:
                self.intervention = get_intervention(conf=self.conf)
                if isinstance(self.intervention, Tracing):
                    self.have_some_humans_download_the_app()

                _ = [h.notify(self.intervention) for h in self.humans]

                print(self.intervention)
                self.conf["GLOBAL_MOBILITY_SCALING_FACTOR"] = tmp_M
                if self.conf.get('COLLECT_TRAINING_DATA'):
                    print("naive risk calculation without changing behavior... Humans notified!")

                humans_notified = True

            # run city testing routine, providing test results for those who need them
            # TODO: running this every hour of the day might not be correct.
            # TODO: testing budget is used up at hour 0 if its small
            self.covid_testing_facility.clear_test_queue()

            all_new_update_messages = []  # accumulate everything here so we can filter if needed
            backup_human_init_risks = {}  # backs up human risks before any update takes place

            # iterate over humans, and if it's their timeslot, then update their state
            for human in self.humans:
                human.check_covid_testing_needs()  # humans can decide to get tested whenever
                if current_day not in human.infectiousness_history_map:
                    # contrarily to risk, infectiousness only changes once a day (human behavior has no impact)
                    human.infectiousness_history_map[current_day] = calculate_average_infectiousness(human)
                assert human.has_app or human._rec_level == -1, \
                    f"human {human.name} has an app ({human.has_app}) but rec_level = {human._rec_level}"
                if not human.has_app or self.env.timestamp.hour not in human.time_slots:
                    continue
                human.initialize_daily_risk(current_day)
                backup_human_init_risks[human] = copy.deepcopy(human.risk_history_map)
                human.run_timeslot_lightweight_jobs(
                    init_timestamp=self.start_time,
                    current_timestamp=self.env.timestamp,
                    personal_mailbox=self.global_mailbox[human.name],
                )

            if isinstance(self.intervention, Tracing) and self.intervention.app:
                # time to run the cluster+risk prediction via transformer (if we need it)
                if self.intervention.risk_model == "transformer" or self.conf.get("COLLECT_TRAINING_DATA"):
                    from covid19sim.models.run import batch_run_timeslot_heavy_jobs
                    self.humans = batch_run_timeslot_heavy_jobs(
                        humans=self.humans,
                        init_timestamp=self.start_time,
                        current_timestamp=self.env.timestamp,
                        global_mailbox=self.global_mailbox,
                        time_slot=self.env.timestamp.hour,
                        conf=self.conf,
                        data_path=outfile,
                        # let's hope there are no collisions on the server with this hash...
                        city_hash=self.hash,
                    )

                # finally, iterate over humans again, and if it's their timeslot, then send update messages
                self.tracker.track_risk_attributes(self.hd)
                update_messages = []
                for human in self.humans:
                    if not human.has_app or self.env.timestamp.hour not in human.time_slots:
                        continue
                    # if we had any encounters for which we have not sent an initial message, do it now
                    update_messages.extend(human.contact_book.generate_initial_updates(
                        current_day_idx=current_day,
                        current_timestamp=self.env.timestamp,
                        risk_history_map=human.risk_history_map,
                        proba_to_risk_level_map=human.proba_to_risk_level_map,
                        tracing_method=human.tracing_method,
                    ))
                    # then, generate risk level update messages for all other encounters (if needed)
                    update_messages.extend(human.contact_book.generate_updates(
                        current_day_idx=current_day,
                        current_timestamp=self.env.timestamp,
                        prev_risk_history_map=human.prev_risk_history_map,
                        curr_risk_history_map=human.risk_history_map,
                        proba_to_risk_level_map=human.proba_to_risk_level_map,
                        update_reason="unknown",  # FIXME got schwacked in hotfix, only used for debugging
                        tracing_method=human.tracing_method,
                    ))
                    human.get_recommendations_level()
                    human.tracing_method.modify_behavior(human)
                    Event.log_risk_update(
                        self.conf['COLLECT_LOGS'],
                        human=human,
                        tracing_description=str(human.tracing_method),
                        prev_risk_history_map=human.prev_risk_history_map,
                        risk_history_map=human.risk_history_map,
                        current_day_idx=current_day,
                        time=self.env.timestamp,
                    )
                    for day_idx, risk_val in human.risk_history_map.items():
                        human.prev_risk_history_map[day_idx] = risk_val

                self.register_new_messages(
                    current_day_idx=current_day,
                    current_timestamp=self.env.timestamp,
                    update_messages=update_messages,
                    prev_human_risk_history_maps=backup_human_init_risks,
                    new_human_risk_history_maps={h: h.risk_history_map for h in self.humans},
                )

            yield self.env.timeout(int(duration))

            if current_day != last_day_idx:
                last_day_idx = current_day
                # Compute the transition matrix of recommendation levels to
                # target distribution of recommendation levels
                self.daily_rec_level_mapping = self.compute_daily_rec_level_mapping(current_day)
                self.cleanup_global_mailbox(self.env.timestamp)
                # TODO: this is an assumption which will break in reality, instead of updating once per day everyone at the same time, it should be throughout the day
                for human in self.humans:
                    human.catch_other_disease_at_random()
                    Event.log_daily(self.conf.get('COLLECT_LOGS'), human, human.env.timestamp)
                self.tracker.increment_day()
                if self.conf.get("USE_GAEN"):
                    print(
                        "cur_day: {}, budget spent: {} / {} ".format(
                            current_day,
                            self.sent_messages_by_day.get(current_day, 0),
                            int(self.conf["n_people"] * self.conf["MESSAGE_BUDGET_GAEN"])
                        ),
                    )

class TestFacility(object):
    """
    Implements queue behavior for tests.
    It keeps a queue of `Human`s who need a test.
    Depending on the daily budget of testing, tests are administered to `Human` according to a scoring function.
    """

    def __init__(self, test_type_preference, max_capacity_per_test_type, env, conf):
        self.test_type_preference = test_type_preference
        self.max_capacity_per_test_type = max_capacity_per_test_type

        self.test_count_today = defaultdict(int)
        self.env = env
        self.conf = conf
        self.test_queue = OrderedSet()
        self.last_date_to_check_tests = self.env.timestamp.date()

    def reset_tests_capacity(self):
        """
        Resets the tests capactiy back to the allowed budget each day.
        """
        if self.last_date_to_check_tests != self.env.timestamp.date():
            self.last_date_to_check_tests = self.env.timestamp.date()
            for k in self.test_count_today.keys():
                self.test_count_today[k] = 0

            # clear queue
            # TODO : check more scenarios about when the person can be removed from a queue
            to_remove = []
            for human in self.test_queue:
                if not any(human.symptoms) and not human.test_recommended:
                    to_remove.append(human)

            _ = [self.test_queue.remove(human) for human in to_remove]

    def get_available_test(self):
        """
        Returns a first type that is available according to preference hierarchy

        See TEST_TYPES in core.yaml

        Returns:
            str: available test_type
        """
        for test_type in self.test_type_preference:
            if self.test_count_today[test_type] < self.max_capacity_per_test_type[test_type]:
                self.test_count_today[test_type] += 1
                return test_type

    def add_to_test_queue(self, human):
        """
        Adds `Human` to the test queue.

        Args:
            human (Human): `Human` object.
        """
        if human in self.test_queue:
            return
        self.test_queue.add(human)

    def clear_test_queue(self):
        """
        It is called at the same frequency as `while` in City.run.
        Triages `Human` in queue to administer tests.
        With probability P_FALSE_NEGATIVE the test will be negative, otherwise it will be positive

        See TEST_TYPES in core.yaml
        """
        # reset here. if reset at end, it results in carry-over of remaining test at the 0th hour.
        self.reset_tests_capacity()
        test_triage = sorted(list(self.test_queue), key=lambda human: -self.score_test_need(human))
        for human in test_triage:
            test_type = self.get_available_test()
            if test_type:
                if human.infection_timestamp is not None:
                    if human.rng.rand() < get_test_false_negative_rate(test_type, human.days_since_covid, human.conf):
                        unobserved_result = 'negative'
                    else:
                        unobserved_result = 'positive'
                else:
                    if human.rng.rand() < self.conf['TEST_TYPES'][test_type]["P_FALSE_POSITIVE"]:
                        unobserved_result = "positive"
                    else:
                        unobserved_result = "negative"

                human.set_test_info(test_type, unobserved_result)  # /!\ sets other attributes related to tests
                self.test_queue.remove(human)

            else:
                # no more tests available
                break

        logging.debug(f"Cleared the test queue for {len(test_triage)} humans. "
                      f"Out of those, {len(test_triage) - len(self.test_queue)} "
                      f"were tested")

    def score_test_need(self, human):
        """
        Score `Human`s according to some criterion. Highest score gets the test first.
        Note: this can be replaced by a better heuristic.

        Args:
            human (Human): `Human` object.

        Returns:
            float: score value indicating chances of `Human` getting a test.
        """
        score = 0

        if 'severe' in human.symptoms:
            score += self.conf['P_TEST_SEVERE']
        elif 'moderate' in human.symptoms:
            score += self.conf['P_TEST_MODERATE']
        elif 'mild' in human.symptoms:
            score += self.conf['P_TEST_MILD']

        if isinstance(human.location, (Hospital, ICU)):
            score += 1

        if human.test_recommended:
            score += 0.3  # @@@@@@ FIXME THIS IS ARBITRARY

        return score

class Location(simpy.Resource):
    """
    Class representing generic locations used in the simulator
    """

    def __init__(self, env, rng, area, name, location_type, lat, lon,
            social_contact_factor, capacity, surface_prob):
        """
        Locations are created with city.create_location(), not instantiated directly

        Args:
            env (covid19sim.Env): Shared environment
            rng (np.random.RandomState): Random number generator
            area (float): Area of the location
            name (str): The location's name
            location_type (str): Location's type, see
            lat (float): Location's latitude
            lon (float): Location's longitude
            social_contact_factor (float): how much people are close to each other
                see contamination_probability() (this scales the contamination pbty)
            capacity (int): Daily intake capacity for the location (infinity if None).
            surface_prob (float): distribution of surface types in the Location. As
                different surface types have different contamination probabilities
                and virus "survival" durations, this will influence the contamination
                of humans at this location.
                Surfaces: aerosol, copper, cardboard, steel, plastic
        """


        if capacity is None:
            capacity = simpy.core.Infinity

        super().__init__(env, capacity)
        self.humans = OrderedSet() #OrderedSet instead of set for determinism when iterating
        self.name = name
        self.rng = np.random.RandomState(rng.randint(2 ** 16))
        self.lat = lat
        self.lon = lon
        self.area = area
        self.location_type = location_type
        self.social_contact_factor = social_contact_factor
        self.env = env
        self.contamination_timestamp = datetime.datetime.min
        self.contaminated_surface_probability = surface_prob
        self.max_day_contamination = 0
        self.is_open_for_business = True

    def infectious_human(self):
        """
        Returns:
            bool: Is there an infectious human currently at that location
        """
        return any([h.is_infectious for h in self.humans])

    def __repr__(self):
        """
        Returns:
            str: Representation of the Location
        """
        return f"{self.name} - occ:{len(self.humans)}/{self.capacity} - I:{self.is_contaminated}"

    def add_human(self, human):
        """
        Adds a human instance to the OrderedSet of humans at the location.
        If they are infectious, then location.contamination_timestamp is set to the
        env's timestamp and the duration of this contamination is set
        (location.max_day_contamination) according to the distribution of surfaces
        (location.contaminated_surface_probability) and the survival of the virus
        per surface type (MAX_DAYS_CONTAMINATION)

        Args:
            human (covid19sim.simulator.Human): The human to add.
        """
        self.humans.add(human)
        if human.is_infectious:
            self.contamination_timestamp = self.env.timestamp
            rnd_surface = float(self.rng.choice(
                    a=human.conf.get("MAX_DAYS_CONTAMINATION"),
                    size=1,
                    p=self.contaminated_surface_probability
            ))
            self.max_day_contamination = max(self.max_day_contamination, rnd_surface)

    def remove_human(self, human):
        """
        Remove a given human from location.human
        /!\ Human is not returned

        Args:
            human (covid19sim.simulator.Human): The human to remove
        """
        self.humans.remove(human)

    @property
    def is_contaminated(self):
        """
        Is the location currently contaminated? This depends on the moment
        when it got contaminated (see add_human()), current time and the
        duration of the contamination (location.max_day_contamination)

        Returns:
            bool: Is the place currently contaminating?
        """
        return self.env.timestamp - self.contamination_timestamp <= datetime.timedelta(days=self.max_day_contamination)

    @property
    def contamination_probability(self):
        """
        Contamination depends on the time the virus has been sitting on a given surface
        (location.max_day_contamination) and is linearly decayed over time.
        Then it is scaled by location.social_contact_factor

        If not location.is_contaminated, return 0.0

        Returns:
            float: probability that a human is contaminated when going to this location.
        """
        if self.is_contaminated:
            lag = (self.env.timestamp - self.contamination_timestamp)
            lag /= datetime.timedelta(days=1)
            p_infection = 1 - lag / self.max_day_contamination # linear decay; &envrionmental_contamination
            return p_infection
        return 0.0

    def __hash__(self):
        """
        Hash of the location is the hash of its name

        Returns:
            int: hash
        """
        return hash(self.name)

    def serialize(self):
        """
        This function serializes the location object by deleting
        non-serializable keys

        Returns:
            dict: serialized location
        """
        s = self.__dict__
        if s.get('env'):
            del s['env']
        if s.get('rng'):
            del s['rng']
        if s.get('_env'):
            del s['_env']
        if s.get('contamination_timestamp'):
            del s['contamination_timestamp']
        if s.get('residents'):
            del s['residents']
        if s.get('humans'):
            del s['humans']
        return s

class Household(Location):
    """
    Household location class, inheriting from covid19sim.base.Location
    """
    def __init__(self, **kwargs):
        """
        Args:
            kwargs (dict): all the args necessary for a Location's init
        """
        super(Household, self).__init__(**kwargs)
        self.residents = []

class Hospital(Location):
    """
    Hospital location class, inheriting from covid19sim.base.Location
    """
    ICU_AREA = 0.10
    ICU_CAPACITY = 0.10
    def __init__(self, **kwargs):
        """
        Create the Hospital and its ICU

        Args:
            kwargs (dict): all the args necessary for a Location's init
        """
        env = kwargs.get('env')
        rng = kwargs.get('rng')
        capacity = kwargs.get('capacity')
        name = kwargs.get("name")
        lat = kwargs.get('lat')
        lon = kwargs.get('lon')
        area = kwargs.get('area')
        surface_prob = kwargs.get('surface_prob')
        social_contact_factor = kwargs.get('social_contact_factor')

        super(Hospital, self).__init__( env=env,
                                        rng=rng,
                                        area=area * (1-self.ICU_AREA),
                                        name=name,
                                        location_type="hospital",
                                        lat=lat,
                                        lon=lon,
                                        social_contact_factor=social_contact_factor,
                                        capacity=math.ceil(capacity* (1- self.ICU_CAPACITY)),
                                        surface_prob=surface_prob,
                                        )
        self.location_contamination = 1
        self.icu = ICU( env=env,
                        rng=rng,
                        area=area * (self.ICU_AREA),
                        name=f"{name}-icu",
                        location_type="hospital-icu",
                        lat=lat,
                        lon=lon,
                        social_contact_factor=social_contact_factor,
                        capacity=math.ceil(capacity* (self.ICU_CAPACITY)),
                        surface_prob=surface_prob,
                        )

    def add_human(self, human):
        """
        Add a human to the Hospital's OrderedSet through the Location's
        default add_human() method + set the human's obs_hospitalized attribute
        is set to True

        Args:
            human (covid19sim.simulator.Human): human to add
        """
        human.obs_hospitalized = True
        super().add_human(human)

    def remove_human(self, human):
        """
        Remove a human from the Hospital's Ordered set.
        On top of Location.remove_human(), the human's obs_hospitalized attribute is
        set to False

        Args:
            human (covid19sim.simulator.Human): human to remove
        """
        human.obs_hospitalized = False
        super().remove_human(human)

class ICU(Location):
    """
    Hospital location class, inheriting from covid19sim.base.Location
    """
    def __init__(self, **kwargs):
        """
        Create a Hospital's ICU Location

        Args:
            kwargs (dict): all the args necessary for a Location's init
        """
        super().__init__(**kwargs)

    def add_human(self, human):
        """
        Add a human to the ICU's OrderedSet through the Location's
        default add_human() method + set the human's obs_hospitalized and
        obs_in_icu attributes are set to True

        Args:
            human (covid19sim.simulator.Human): human to add
        """
        human.obs_hospitalized = True
        human.obs_in_icu = True
        super().add_human(human)

    def remove_human(self, human):
        """
        Remove a human from the ICU's Ordered set.
        On top of Location.remove_human(), the human's obs_hospitalized and
        obs_in_icu attributes are set to False

        Args:
            human (covid19sim.simulator.Human): human to remove
        """
        human.obs_hospitalized = False
        human.obs_in_icu = False
        super().remove_human(human)

class Event:
    """
    [summary]
    """
    test = 'test'
    encounter = 'encounter'
    encounter_message = 'encounter_message'
    risk_update = 'risk_update'
    contamination = 'contamination'
    recovered = 'recovered'
    static_info = 'static_info'
    visit = 'visit'
    daily = 'daily'

    @staticmethod
    def members():
        """
        DEPRECATED
        """
        return [Event.test, Event.encounter, Event.contamination, Event.static_info, Event.visit, Event.daily]

    @staticmethod
    def log_encounter(COLLECT_LOGS, human1, human2, location, duration, distance, infectee, p_infection, time):
        """
        Logs the encounter between `human1` and `human2` at `location` for `duration`
        while staying at `distance` from each other. If infectee is not None, it is
        either human1.name or human2.name.

        Each of the two humans gets its `events` attribute appended whit a dictionnary
        describing the encounter:

        human.events.append({
                'human_id':human.name,
                'event_type':Event.encounter,
                'time':time,
                'payload':{
                    'observed': obs_payload,  # None if one of the humans does not have
                                              # the app. Otherwise contains the observed
                                              # data: lat, lon, location_type
                    'unobserved':unobs_payload  # unobserved data, see loc_unobs_keys and
                                                # h_unobs_keys
                }
        })


        Args:
            COLLECT_LOGS (bool): Log the event in a file if True
            human1 (covid19sim.simulator.Human): One of the encounter's 2 humans
            human2 (covid19sim.simulator.Human): One of the encounter's 2 humans
            location (covid19sim.base.Location): Where the encounter happened
            duration (int): duration of encounter
            distance (float): distance between people (TODO: meters? cm?)
            infectee (str | None): name of the human which is infected, if any.
                None otherwise
            p_infection (float): probability for the infectee of getting infected
            time (datetime.datetime): timestamp of encounter
        """
        if COLLECT_LOGS:
            h_obs_keys   = ['obs_hospitalized', 'obs_in_icu',
                            'obs_lat', 'obs_lon']

            h_unobs_keys = ['carefulness', 'viral_load', 'infectiousness',
                            'symptoms', 'is_exposed', 'is_infectious',
                            'infection_timestamp', 'is_really_sick',
                            'is_extremely_sick', 'sex',  'wearing_mask', 'mask_efficacy',
                            'risk', 'risk_level', 'rec_level']

            loc_obs_keys = ['location_type', 'lat', 'lon']
            loc_unobs_keys = ['contamination_probability', 'social_contact_factor']

            obs, unobs = [], []

            same_household = (human1.household.name == human2.household.name) & (location.name == human1.household.name)
            for human in [human1, human2]:
                o = {key:getattr(human, key) for key in h_obs_keys}
                obs.append(o)
                u = {key:getattr(human, key) for key in h_unobs_keys}
                u['human_id'] = human.name
                u['location_is_residence'] = human.household == location
                u['got_exposed'] = infectee == human.name if infectee else False
                u['exposed_other'] = infectee != human.name if infectee else False
                u['same_household'] = same_household
                u['infectiousness_start_time'] = None if not u['got_exposed'] else human.infection_timestamp + datetime.timedelta(days=human.infectiousness_onset_days)
                unobs.append(u)

            loc_obs = {key:getattr(location, key) for key in loc_obs_keys}
            loc_unobs = {key:getattr(location, key) for key in loc_unobs_keys}
            loc_unobs['location_p_infection'] = location.contamination_probability / location.social_contact_factor
            other_obs = {'duration':duration, 'distance':distance}
            other_unobs = {'p_infection':p_infection}
            both_have_app = human1.has_app and human2.has_app
            for i, human in [(0, human1), (1, human2)]:
                if both_have_app:
                    obs_payload = {**loc_obs, **other_obs, 'human1':obs[i], 'human2':obs[1-i]}
                    unobs_payload = {**loc_unobs, **other_unobs, 'human1':unobs[i], 'human2':unobs[1-i]}
                else:
                    obs_payload = {}
                    unobs_payload = { **loc_obs, **loc_unobs, **other_obs, **other_unobs, 'human1':{**obs[i], **unobs[i]},
                                        'human2': {**obs[1-i], **unobs[1-i]} }

            human.events.append({
                'human_id':human.name,
                'event_type':Event.encounter,
                'time':time,
                'payload':{'observed':obs_payload, 'unobserved':unobs_payload}
            })

        logging.info(f"{time} - {human1.name} and {human2.name} {Event.encounter} event")
        logging.debug("{time} - {human1.name}{h1_infectee} "
                      "encountered {human2.name}{h2_infectee} "
                      "for {duration:.2f}min at ({location.lat}, {location.lon}) "
                      "and stayed at {distance:.2f}cm. The probability of getting "
                      "infected was {p_infection:.3f}"
                      .format(time=time,
                              human1=human1,
                              h1_infectee=' (infectee)' if infectee == human1.name else '',
                              human2=human2,
                              h2_infectee=' (infectee)' if infectee == human2.name else '',
                              duration=duration,
                              location=location,
                              distance=distance,
                              p_infection=p_infection if p_infection else 0.0
                      ))

    def log_encounter_messages(COLLECT_LOGS, human1, human2, location, duration, distance, time):
        """
        Logs the encounter between `human1` and `human2` at `location` for `duration`
        while staying at `distance` from each other. If infectee is not None, it is
        either human1.name or human2.name.

        Each of the two humans gets its `events` attribute appended whit a dictionnary
        describing the encounter:

        human.events.append({
                'human_id':human.name,
                'event_type':Event.encounter,
                'time':time,
                'payload':{
                    'observed': obs_payload,  # None if one of the humans does not have
                                              # the app. Otherwise contains the observed
                                              # data: lat, lon, location_type
                    'unobserved':unobs_payload  # unobserved data, see loc_unobs_keys and
                                                # h_unobs_keys
                }
        })


        Args:
            COLLECT_LOGS (bool): Log the event in a file if True
            human1 (covid19sim.simulator.Human): One of the encounter's 2 humans
            human2 (covid19sim.simulator.Human): One of the encounter's 2 humans
            location (covid19sim.base.Location): Where the encounter happened
            duration (int): duration of encounter
            distance (float): distance between people (TODO: meters? cm?)
            infectee (str | None): name of the human which is infected, if any.
                None otherwise
            time (datetime.datetime): timestamp of encounter
        """
        if COLLECT_LOGS:
            h_obs_keys   = ['obs_hospitalized', 'obs_in_icu',
                            'obs_lat', 'obs_lon']

            h_unobs_keys = ['carefulness', 'viral_load', 'infectiousness',
                            'symptoms', 'is_exposed', 'is_infectious',
                            'infection_timestamp', 'is_really_sick',
                            'is_extremely_sick', 'sex',  'wearing_mask', 'mask_efficacy',
                            'risk', 'risk_level', 'rec_level']

            loc_obs_keys = ['location_type', 'lat', 'lon']
            loc_unobs_keys = ['contamination_probability', 'social_contact_factor']

            obs, unobs = [], []

            same_household = (human1.household.name == human2.household.name) & (location.name == human1.household.name)
            for human in [human1, human2]:
                o = {key:getattr(human, key) for key in h_obs_keys}
                obs.append(o)
                u = {key:getattr(human, key) for key in h_unobs_keys}
                u['human_id'] = human.name
                u['location_is_residence'] = human.household == location
                u['same_household'] = same_household
                unobs.append(u)

            loc_obs = {key:getattr(location, key) for key in loc_obs_keys}
            loc_unobs = {key:getattr(location, key) for key in loc_unobs_keys}
            loc_unobs['location_p_infection'] = location.contamination_probability / location.social_contact_factor
            other_obs = {'duration':duration, 'distance':distance}
            both_have_app = human1.has_app and human2.has_app
            for i, human in [(0, human1), (1, human2)]:
                if both_have_app:
                    obs_payload = {**loc_obs, **other_obs, 'human1':obs[i], 'human2':obs[1-i]}
                    unobs_payload = {**loc_unobs, 'human1':unobs[i], 'human2':unobs[1-i]}
                else:
                    obs_payload = {}
                    unobs_payload = { **loc_obs, **loc_unobs, **other_obs, 'human1':{**obs[i], **unobs[i]},
                                        'human2': {**obs[1-i], **unobs[1-i]} }

            human.events.append({
                'human_id':human.name,
                'event_type':Event.encounter_message,
                'time':time,
                'payload':{'observed':obs_payload, 'unobserved':unobs_payload}
            })

        logging.info(f"{time} - {human1.name} and {human2.name} {Event.encounter_message} event")
        logging.debug("{time} - {human1.name} and {human2.name} exchanged encounter "
                      "messages for {duration:.2f}min at ({location.lat}, {location.lon}) "
                      "and stayed at {distance:.2f}cm"
                      .format(time=time,
                              human1=human1,
                              human2=human2,
                              duration=duration,
                              location=location,
                              distance=distance))

    def log_risk_update(COLLECT_LOGS, human, tracing_description,
                        prev_risk_history_map, risk_history_map, current_day_idx,
                        time):
        if COLLECT_LOGS:
            human.events.append({
                'human_id':human.name,
                'event_type':Event.risk_update,
                'time':time,
                'payload':{
                    'observed': {
                        'tracing_description': tracing_description,
                        'prev_risk_history_map': prev_risk_history_map,
                        'risk_history_map': risk_history_map,
                    }
                }
            })

        logging.info(f"{time} - {human.name} {Event.risk_update} event")
        logging.debug(f"{time} - {human.name} updated his risk from "
                      f"{prev_risk_history_map[current_day_idx]} to "
                      f"{risk_history_map[current_day_idx]} following "
                      f"{tracing_description} rules")

    @staticmethod
    def log_test(COLLECT_LOGS, human, time):
        """
        Adds an event to a human's `events` list if COLLECT_LOGS is True.
        Events contains the test resuts time, reported_test_result,
        reported_test_type, test_result_validated, test_type, test_result
        split across observed and unobserved data.

        Args:
            COLLECT_LOGS ([type]): [description]
            human (covid19sim.simulator.Human): Human whose test should be logged
            time (datetime.datetime): Event's time
        """
        if COLLECT_LOGS:
            human.events.append(
                {
                    'human_id': human.name,
                    'event_type': Event.test,
                    'time': time,
                    'payload': {
                        'observed':{
                            'result': human.reported_test_result,
                            'test_type':human.reported_test_type,
                            'validated_test_result':human.test_result_validated
                        },
                        'unobserved':{
                            'test_type':human.test_type,
                            'result': human.test_result
                        }

                    }
                }
            )
        logging.info(f"{time} - {human.name} {Event.test} event")
        logging.debug(f"{time} - {human.name} tested {human.test_result}.")

    @staticmethod
    def log_daily(COLLECT_LOGS, human, time):
        """
        Adds an event to a human's `events` list containing daily health information
        like symptoms, infectiousness and viral_load.

        Args:
            COLLECT_LOGS ([type]): [description]
            human (covid19sim.simulator.Human): Human who's health should be logged
            time (datetime.datetime): Event time
        """
        if COLLECT_LOGS:
            human.events.append(
                {
                    'human_id': human.name,
                    'event_type': Event.daily,
                    'time': time,
                    'payload': {
                        'observed':{
                            "reported_symptoms": human.obs_symptoms
                        },
                        'unobserved':{
                            'infectiousness': human.infectiousness,
                            "viral_load": human.viral_load,
                            "all_symptoms": human.all_symptoms,
                            "covid_symptoms":human.covid_symptoms,
                            "flu_symptoms":human.flu_symptoms,
                            "cold_symptoms":human.cold_symptoms
                        }
                    }
                }
            )
        logging.info(f"{time} - {human.name} {Event.daily} event")

    @staticmethod
    def log_exposed(COLLECT_LOGS, human, source, p_infection, time):
        """
        [summary]

        Args:
            COLLECT_LOGS ([type]): [description]
            human ([type]): [description]
            source ([type]): [description]
            p_infection (float): probability for the infectee of getting infected
            time ([type]): [description]
        """
        if COLLECT_LOGS:
            human.events.append(
                {
                    'human_id': human.name,
                    'event_type': Event.contamination,
                    'time': time,
                    'payload': {
                        'observed':{
                        },
                        'unobserved':{
                          'exposed': True,
                          'source':source.name,
                          'source_is_location': 'human' not in source.name,
                          'source_is_human': 'human' in source.name,
                          'infectiousness_start_time': human.infection_timestamp + datetime.timedelta(days=human.infectiousness_onset_days),
                          'p_infection': p_infection
                        }
                    }
                }
            )
        logging.info(f"{time} - {human.name} {Event.contamination} event")
        logging.debug("{time} - {human.name} was contaminated by {source.name}. "
                      "The probability of getting infected was {p_infection:.3f}"
                      .format(time=time,
                              human=human,
                              source=source,
                              p_infection=p_infection))

    @staticmethod
    def log_recovery(COLLECT_LOGS, human, time, death):
        """
        [summary]

        Args:
            COLLECT_LOGS ([type]): [description]
            human ([type]): [description]
            time ([type]): [description]
            death ([type]): [description]
        """
        if COLLECT_LOGS:
            human.events.append(
                {
                    'human_id': human.name,
                    'event_type': Event.recovered,
                    'time': time,
                    'payload': {
                        'observed':{
                        },
                        'unobserved':{
                            'recovered': not death,
                            'death': death
                        }
                    }
                }
            )
        logging.info(f"{time} - {human.name} {Event.recovered} event")
        logging.debug(f"{time} - {human.name} recovered and is {'' if death else 'not'} {death}.")


    @staticmethod
    def log_static_info(COLLECT_LOGS, city, human, time):
        """
        [summary]

        Args:
            COLLECT_LOGS ([type]): [description]
            city ([type]): [description]
            human ([type]): [description]
            time ([type]): [description]
        """
        h_obs_keys = ['obs_preexisting_conditions',  "obs_age", "obs_sex", "obs_is_healthcare_worker"]
        h_unobs_keys = ['preexisting_conditions', "age", "sex", "is_healthcare_worker"]
        obs_payload = {key:getattr(human, key) for key in h_obs_keys}
        unobs_payload = {key:getattr(human, key) for key in h_unobs_keys}

        if human.workplace.location_type in ['healthcare', 'store', 'misc', 'senior_residency']:
            obs_payload['n_people_workplace'] = 'many people'
        elif "workplace" == human.workplace.location_type:
            obs_payload['n_people_workplace'] = 'few people'
        else:
            obs_payload['n_people_workplace'] = 'no people outside my household'

        obs_payload['household_size'] = len(human.household.residents)

        event = {
                'human_id': human.name,
                'event_type':Event.static_info,
                'time':time,
                'payload':{
                    'observed': obs_payload,
                    'unobserved':unobs_payload
                }
            }
        if COLLECT_LOGS:
            human.events.append(event)
        logging.info(f"{time} - {human.name} {Event.static_info} event")
        logging.debug(f"{time} - {human} static info:\n{event}")


class EmptyCity(City):
    """
    An empty City environment (no humans or locations) that the user can build with
    externally defined code.  Useful for controlled scenarios and functional testing
    """

    def __init__(self, env, rng, x_range, y_range, conf):
        """

        Args:
            env (simpy.Environment): [description]
            rng (np.random.RandomState): Random number generator
            x_range (tuple): (min_x, max_x)
            y_range (tuple): (min_y, max_y)
            conf (dict): yaml experiment configuration
        """
        self.conf = conf
        self.env = env
        self.rng = np.random.RandomState(rng.randint(2 ** 16))
        self.x_range = x_range
        self.y_range = y_range
        self.total_area = (x_range[1] - x_range[0]) * (y_range[1] - y_range[0])
        self.n_people = 0

        self.test_type_preference = list(zip(*sorted(conf.get("TEST_TYPES").items(), key=lambda x:x[1]['preference'])))[0]
        self.max_capacity_per_test_type = {
            test_type: max([int(conf['TEST_TYPES'][test_type]['capacity'] * self.n_people), 1])
            for test_type in self.test_type_preference
        }

        self.daily_target_rec_level_dists = None
        self.covid_testing_facility = TestFacility(self.test_type_preference, self.max_capacity_per_test_type, env, conf)

        # Get the test type with the lowest preference?
        # TODO - EM: Should this rather sort on 'preference' in descending order?
        self.test_type_preference = list(
            zip(
                *sorted(
                    self.conf.get("TEST_TYPES").items(),
                    key=lambda x:x[1]['preference']
                )
            )
        )[0]

        self.humans = []
        self.households = OrderedSet()
        self.stores = []
        self.senior_residencys = []
        self.hospitals = []
        self.miscs = []
        self.parks = []
        self.schools = []
        self.workplaces = []
        self.global_mailbox: SimulatorMailboxType = defaultdict(dict)

    @property
    def start_time(self):
        return datetime.datetime.fromtimestamp(self.env.ts_initial)

    def initWorld(self):
        """
        After adding humans and locations to the city, execute this function to finalize the City
        object in preparation for simulation.
        """
        self.log_static_info()

        print("Computing preferences")
        self._compute_preferences()
        self.tracker = Tracker(self.env, self)
        # self.tracker.track_initialized_covid_params(self.humans)
        self.intervention = None
