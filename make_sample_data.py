"""
Generate synthetic exports so build_journey.py can be run without any real data.

    python make_sample_data.py          # writes the four CSVs + an Inner Circle list
    python build_journey.py             # builds client_journey.db from them

Everything here is made up: names come from short word lists, emails are
@example.com, and account numbers are random. The column names match what
build_journey.py reads.
"""

import csv
import random

random.seed(7)

FIRST = ["Aarav", "Maya", "Liam", "Sofia", "Noah", "Zara", "Ethan", "Ines",
         "Kenji", "Amara", "Lucas", "Priya", "Omar", "Elena", "Diego", "Hana"]
LAST = ["Shah", "Costa", "Nguyen", "Okafor", "Silva", "Kim", "Novak", "Haddad",
        "Tanaka", "Moreau", "Ivanov", "Mensah", "Garcia", "Larsen"]
COUNTRIES = ["IN", "BR", "NG", "DE", "VN", "AE", "MX", "PH", "ZA", "ID"]
SIZES = ["5K", "10K", "25K", "50K", "100K"]

N_CLIENTS = 300


def write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"  {path}: {len(rows)} rows")


def main():
    customers, progress, passes, payouts, inner = [], [], [], [], []
    next_login = 2200000
    payout_id = 9000

    for i in range(1, N_CLIENTS + 1):
        cnum = f"C{i:07d}"                      # zero-padded, like the portal export
        first, last = random.choice(FIRST), random.choice(LAST)
        email = f"{first.lower()}.{last.lower()}{i}@example.com"
        customers.append([cnum, 50000 + i, first, last, email,
                          random.choice(COUNTRIES), "false",
                          "true" if random.random() < 0.02 else "false",
                          f"2025-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}",
                          random.randint(1, 4), random.randint(0, 900)])

        # how far this client got: 0 registered only ... 4 paid out
        stage = random.choices([0, 1, 2, 3, 4], weights=[30, 30, 18, 14, 8])[0]
        if stage == 0:
            continue

        size = random.choice(SIZES)
        next_login += 1
        c1 = next_login
        row = {"customerNumber": cnum}
        if stage == 1:
            status = random.choice(["Active", "Breached"])
            row.update(planType1=f"C-1 {size}", loginType1=c1, statusType1=status)
        else:
            next_login += 1
            f_login = next_login
            row.update(planType1=f"C-1 {size}", loginType1=c1,
                       statusType1="Hit Profit Target")
            passes.append([email, c1, f"C-1 {size}", "false", ""])
            if stage >= 3:
                row.update(planType2=f"F {size}", loginType2=f_login,
                           statusType2=random.choice(["Active", "Breached"]))
                passes[-1][3] = "true"
            if stage == 4:
                payout_id += 1
                payouts.append([email, f_login, payout_id,
                                f"2026-{random.randint(1, 9):02d}-{random.randint(1, 28):02d}"])
                if random.random() < 0.25:
                    inner.append([email, cnum])
        progress.append(row)

    # a few pending payouts (no CompletedDate) - these must NOT grant the tier
    for email, *_ in random.sample(passes, k=min(5, len(passes))):
        payout_id += 1
        payouts.append([email, "", payout_id, ""])

    print("writing synthetic exports")
    write("all_customers.csv",
          ["customerNumber", "customerId", "firstName", "lastName", "email",
           "country", "isAffiliate", "isBlackLister", "insertedCST",
           "loyaltyLevelId", "loyaltyPoints"], customers)

    cols = ["customerNumber"] + [f"{k}Type{n}" for n in (1, 2, 3, 4)
                                 for k in ("plan", "login", "status")]
    write("account_progress.csv", cols,
          [[r.get(c, "") for c in cols] for r in progress])
    write("pass_analytics.csv",
          ["email", "accountNumber", "planName", "everFunded", "riskflags"], passes)
    write("payouts.csv", ["email", "login", "PayoutId", "CompletedDate"], payouts)
    write("_Inner_Circle_Data.csv", ["Email", "Customer Number"], inner)


if __name__ == "__main__":
    main()
