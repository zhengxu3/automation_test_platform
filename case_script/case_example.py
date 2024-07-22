class CaseExample:
    def __init__(self):
        # Initialize case-specific resources or state here
        self.step = 1
        print(f"Initialized CaseAnotherExample1111111 with step {self.step}")
        self.step += 1


    def case_run_example(self):
        print("Running case_run_example in CaseExample")

    def case_run_another(self):
        print("Running case_run_another in CaseExample")

    def case_run_match_1to3(self, **kwargs):
        uri = ''
        print(f"Running case_run_match_1to3 in CaseExample: {kwargs} ")