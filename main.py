from draw import Draw

if __name__ == "__main__":

    players = []

    while True:
        try:
            player = str(input(f"Player{len(players)+1}: Enter Name\n\n"))
            players.append(player)
            if len(players) >= 2:
                addAnotherPlayer = str(input("Do you want to add another player? (y/n): "))
                if addAnotherPlayer.lower() == "y": continue
                else: break

        except EOFError:
            print("Invalid")


    while True:
        gamemode = int(input("\nChoose gamemode: snake (1), weighted (2) or random (3) "))
        if gamemode == 3:
            mode = "random"
            break
        elif gamemode == 1:
            mode = "snake"
            break
        else:
            mode = "weighted"
            break


    while True:
        leftover_option = int(input("\nChoose leftover: drop (1) or pool (2)"))
        if leftover_option == 1:
            leftover = "drop"
            break
        else:
            leftover = "pool"
            break


    LEFTOVER = "pool"  # "drop" | "pool"
    T1_CAP = None  # weighted only: cap favourites per player, e.g. 1
    SEED = None  # set None for a different draw every run


    draw = Draw(mode=mode, leftover_policy=leftover, t1_cap=T1_CAP, seed=SEED)
    draw.add_players(players)
    draw.add_all_teams("teams.json")
    draw.sort_teams_to_players()
    print(draw.summary())