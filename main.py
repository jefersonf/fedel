import argparse
import logging
import copy
import time
import os

import torch
import numpy as np

from collections import defaultdict

import ensemble_based as ebl
import fedavg as awl
import utils


def ensemble_based_fl(train_dl, val_dl, test_dl, args, logger):
    """Ensemble-based Learning process."""

    # < REPORT RELATED CODE > ---------------------------------------------------------------------
    log_path = utils.create_logdir(os.path.join(args.log_dir, "ensemble-based-learning"))
    log_client = defaultdict(list)
    log_server = defaultdict(list)
    # </ REPORT RELATED CODE > --------------------------------------------------------------------

    logger.info('Model architectures')
    clients = {}
    for i in range(args.n_clients):
        archtype, model = ebl.DEFAULT_ML_MODELS[(i + args.model_alocation) % len(ebl.DEFAULT_ML_MODELS)]
        clients[i] = ebl.Client(model, client_id=i, archtype=archtype, verbose=args.verbose)
        logger.info(f'Client{i} archtype: {archtype}')
    
    client_datapoints = {i: np.array([], dtype='int64') for i in range(args.n_clients)}
    # Init server
    server = ebl.Server()
    logger.info('Server is initialized')

    for round_id in range(args.rounds):
        logger.info(f'########## Communication round {round_id} ##########')
        for i in clients:
            if round_id:
                clients[i].set_ensemble_model(copy.deepcopy(server.shared_ensemble))
                logger.info(f'Client{i} gets the ensemble model')
            
            features, target = train_dl[i]
            round_datapoints = utils.datapoints_loader(target, mode=args.data_distrib_mode, 
                exclude=np.array([]), batch_size=(features.shape[0] // args.rounds))
            client_datapoints[i] = np.concatenate((client_datapoints[i], round_datapoints), axis=0)

            logger.info(f'Client{i} trains locally (datapoints={len(client_datapoints[i])}, new={len(round_datapoints)})')

            start_tm = time.time()
            clients[i].fit(features.loc[client_datapoints[i],], target.loc[client_datapoints[i],]) 

            # < REPORT RELATED CODE > -------------------------------------------------------------
            log_client["TrainingTime"].append(time.time() - start_tm)

            val_acc = clients[i].evaluate(*val_dl[i])

            start_tm = time.time()
            test_acc = clients[i].evaluate(*test_dl[0])
            log_client["InferenceTime"].append(time.time() - start_tm)

            val_micro_auc, val_macro_auc = clients[i].evaluate(*val_dl[i], metric="AUC")
            test_micro_auc, test_macro_auc = clients[i].evaluate(*test_dl[0], metric="AUC")

            logger.info(f'Client{i} accuracy on validation set: {val_acc:.3f}')
            logger.info(f'Client{i} accuracy on test set: {test_acc:.3f}')

            if round_id:
                clients[i].update_local_scores(*val_dl[i])
                logger.info(f'Client{i} updates the scores of his local copy of shared model')

            log_client["LocalValAccuracy"].append(val_acc)
            log_client["LocalTestAccuracy"].append(test_acc)
            log_client["LocalValMicroAUC"].append(val_micro_auc)
            log_client["LocalValMacroAUC"].append(val_macro_auc)
            log_client["TestMicroAUC"].append(test_micro_auc)
            log_client["TestMacroAUC"].append(test_macro_auc)
            log_client["RoundId"].append(round_id)
            log_client["ModelArchType"].append(ebl.DEFAULT_ML_MODELS[i % len(ebl.DEFAULT_ML_MODELS)][0])
            log_client["ClientId"].append(i)
            log_client["TotalDataPoints"].append(len(client_datapoints[i]))
            log_client["NewDataPoints"].append(len(round_datapoints))
            # </ REPORT RELATED CODE > ------------------------------------------------------------

        if round_id:
            server.update_ensemble(clients, client_datapoints)
            logger.info('Server updates ensemble model')
        else:
            server.set_shared_model(ebl.EnsembleModel(clients, enable_grouping=args.enable_grouping))
            logger.info('A shared model (ensemble) is created')

        # evaluating shared model
        test_features, test_targets = test_dl[0]

        start_tm = time.time()
        server_acc = server.shared_ensemble.evaluate(test_features, test_targets)
        log_server["InferenceTime"].append((time.time() - start_tm))

        server_scores = [float(f"{w:.3f}") for w in server.shared_ensemble.scores]
        precision, recall, micro_auc, macro_auc, cm = server.shared_ensemble.compute_metrics(*test_dl[0], args,round_id,  label=f"Round{round_id}")

        logger.info(f"Ensemble accuracy on global test set: {server_acc:.3f}")
        logger.info(f"Ensemble models' scores: {server_scores}")

    # < REPORT RELATED CODE > ---------------------------------------------------------------------
        log_server["RoundId"].append(round_id)
        log_server["LearningType"].append("EBL")
        log_server["TestAccuracy"].append(server_acc)
        log_server["Precision"].append(precision)
        log_server["Recall"].append(recall)
        log_server["TestMicroAUC"].append(micro_auc)
        log_server["TestMacroAUC"].append(macro_auc)

        server_scores = server.shared_ensemble.get_scores()
        for j in range(args.n_clients):
            log_server[f"P{j}"].append(server_scores[j])
        
        for k in range(test_dl[0][1].unique().shape[0]**2):
            log_server[f"CM{k}"].append(cm[k])
    # </ REPORT RELATED CODE > --------------------------------------------------------------------

    utils.save_logs(log_client, os.path.join(log_path, f"clients_ebl_history_{args.tag}.csv"))
    utils.save_logs(log_server, os.path.join(log_path, f"server_ebl_history_{args.tag}.csv"))


def averaging_weights_fl(train_dl, val_dl, test_dl, args, logger):
    """Function to manage FedAvg process. """

    # < REPORT RELATED CODE > ---------------------------------------------------------------------
    log_path = utils.create_logdir(os.path.join(args.log_dir, "averaging-weights-learning"))
    log_client = defaultdict(list)
    log_server = defaultdict(list)
    # </ REPORT RELATED CODE > ---------------------------------------------------------------------

    n_features = train_dl[0][0].shape[1]
    output_size = test_dl[0][1].unique().shape[0]
    # output_size -= (output_size == 2) # reduce to binary case if classes = 2

    global_model = awl.utils.build_network(args.model_type, n_features, output_size)
    train_params = awl.utils.define_nn_params(args, output_size)
    init_weights = global_model.state_dict()

    logger.info('Model architectures')
    clients = {}
    for i in range(args.n_clients):
        nn_model = awl.utils.build_network(args.model_type, n_features, output_size)
        nn_model.load_state_dict(copy.deepcopy(init_weights))
        clients[i] = awl.Client(model=nn_model, client_id=i, params=train_params, logger=logger)
        logger.info(f'Client{i} archtype: NeuralNetwork-{args.model_type}')
            
    server = awl.Server()
    client_datapoints = {i: np.array([], dtype='int64') for i in range(args.n_clients)}

    logger.info('Server is initialized')
    for round_id in range(args.rounds):
        logger.info(f'########## Communication round {round_id} ##########')
        for i in clients:
            features, target = train_dl[i]
            round_datapoints = utils.datapoints_loader(target, mode=args.data_distrib_mode, 
                exclude=client_datapoints[i], batch_size=(features.shape[0] // args.rounds))
            client_datapoints[i] = np.concatenate((client_datapoints[i], round_datapoints), axis=0)

            logger.info(f'Client{i} trains locally (datapoints={len(client_datapoints[i])}, new={len(round_datapoints)})')

            start_tm = time.time()
            clients[i].train_local_model(features.loc[client_datapoints[i],], target.loc[client_datapoints[i],])

            # < REPORT RELATED CODE > -------------------------------------------------------------
            log_client["TrainingTime"].append(time.time() - start_tm)
            
            val_acc = clients[i].evaluate_local_model(*val_dl[i])

            start_tm = time.time()
            test_acc = clients[i].evaluate_local_model(*test_dl[0])
            log_client["InferenceTime"].append((time.time() - start_tm))

            val_micro_auc, val_macro_auc = clients[i].evaluate_local_model(*val_dl[i], metric="AUC")
            test_micro_auc, test_macro_auc = clients[i].evaluate_local_model(*test_dl[0], metric="AUC")

            logger.info(f'Client{i} accuracy on validation set: {val_acc:.3f}')
            logger.info(f'Client{i} accuracy on test set: {test_acc:.3f}')

            log_client["LocalValAccuracy"].append(val_acc)
            log_client["LocalTestAccuracy"].append(test_acc)
            log_client["LocalValMicroAUC"].append(val_micro_auc)
            log_client["LocalValMacroAUC"].append(val_macro_auc)
            log_client["TestMicroAUC"].append(test_micro_auc)
            log_client["TestMacroAUC"].append(test_macro_auc)
            log_client["RoundId"].append(round_id)
            log_client["ModelArchType"].append(f"NeuralNetwork-{args.model_type}")
            log_client["ClientId"].append(i)
            log_client["TotalDataPoints"].append(len(client_datapoints[i]))
            log_client["NewDataPoints"].append(len(round_datapoints))
            # </ REPORT RELATED CODE > ------------------------------------------------------------

        if round_id == 0:
            server.set_model_archtype(type(clients[0]))
        
        server.update_global_model(clients, client_datapoints)
        server_avg_datapoints = [float(f"{w:.3f}") for w in server._averaged_datapoint_weights]
        logger.info('Server updates global model')
        logger.info(f'Averaged datapoints weights: {server_avg_datapoints}')

        start_tm = time.time()
        test_features, test_targets = test_dl[0]
        server_acc = server.evaluate(clients, test_features, test_targets)
        log_server["InferenceTime"].append((time.time() - start_tm) / test_targets.shape[0])

        precision, recall, micro_auc, macro_auc, cm = server.compute_metrics(clients, *test_dl[0], args, label=f"Round{round_id}")

        logger.info(f"Global accuracy on test set: {server_acc:.3f}")

        # < REPORT RELATED CODE > ---------------------------------------------------------------------
        log_server["RoundId"].append(round_id)
        log_server["LearningType"].append("FedAvg")
        log_server["TestAccuracy"].append(server_acc)
        log_server["Precision"].append(precision)
        log_server["Recall"].append(recall)
        log_server["TestMicroAUC"].append(micro_auc)
        log_server["TestMacroAUC"].append(macro_auc)
        
        for k in range(test_dl[0][1].unique().shape[0]**2):
            log_server[f"CM{k}"].append(cm[k])
        # </ REPORT RELATED CODE > --------------------------------------------------------------------

    utils.save_logs(log_client, os.path.join(log_path, f"clients_awl_history_{args.tag}.csv"))
    utils.save_logs(log_server, os.path.join(log_path, f"server_awl_history_{args.tag}.csv"))


def build_parser():
    parser = argparse.ArgumentParser(description="Ensemble-based Learning process simulator")
    parser.add_argument("--rounds", type=int, default=10, help='Communication rounds.')
    parser.add_argument("--epochs", type=int, default=5, help="Client training epochs.")
    parser.add_argument("--clients", dest="n_clients", type=int, default=3, 
        help="Number of participating clients.")
    parser.add_argument("--data-path", dest="data_path", help="Path to training data.")
    parser.add_argument("--data-split", dest="data_split", nargs="+", type=float, 
        default=[.8, .1, .1],
        help="Train/Val/Test data split (default is 0.8, 0.1, and 0.1, respectively)")
    parser.add_argument("--dirichlet-alpha", dest="dirichlet_alpha", type=float, default=100, 
        help="Alpha value of Dirichlet distribution of training data (default is 100)")
    parser.add_argument("--target", default="label",
        help="Target column name to predict (default is label).")    
    parser.add_argument("--dataset-name", default="unknown_dataset", help="Dataset name.")
    parser.add_argument("--data_distrib_mode", default="uniform", 
        help='Data distribution mode over rounds.')
    parser.add_argument("-v", "--verbose", action="store_true", default=False, 
        help="Sets verbose mode.")
    parser.add_argument("--fedavg", action="store_true", default=False, 
        help="Sets Federated Averaging Algorithm - FedAvg (deafult is Ensemble-based Learning).")
    parser.add_argument("--model_type", type=str, default="A", 
        help="Define wich NN model will be used for federated task")
    parser.add_argument("--lr",type=float, help="lr for optimizer function", default=0.01)
    parser.add_argument("--train_batch_size", type=int, default=32, help="batch size for train NN model", )
    parser.add_argument("--evaluate_batch_size", type=int, default=64, help="batch size for test/val NN model")
    parser.add_argument("--enable_grouping", action="store_true", default=False, 
        help='Enables the grouping of models of the same arch.')
    parser.add_argument("--model_alocation", type=int,  default=0, 
        help="Set to modify the beginning of the circular list")
    parser.add_argument("--log-dir", dest="log_dir", default="./logs")
    parser.add_argument("--tag", default="", help="Tag to add in report files")
    parser.add_argument("--seed", type=int, default=1, help="Random seed value.")

    return parser.parse_args()


if __name__ == "__main__":

    args = build_parser()

    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    
    utils.create_logdir(args.log_dir)
    
    if args.verbose:
        logging.basicConfig(level=logging.INFO)
    else:
        ltype = "awl" if args.fedavg else "ebl"
        logging.basicConfig(level=logging.INFO, filename=f"{args.log_dir}/{ltype}_{args.tag}.log", filemode='w')
    logger = logging.getLogger()
    
    train, val, test = utils.data_partition_split(args.data_path, args.data_split, target_column=args.target)
    train_dl = utils.data_partition_loader(train, args.n_clients, dirichlet_alpha=args.dirichlet_alpha)
    val_dl = utils.data_partition_loader(val, args.n_clients)
    test_dl = utils.data_partition_loader(test, 1) # global test set

    logger.info('Partitioning data')
    for i in range(args.n_clients):
        logger.info(f'Client{i}: train={train_dl[i][0].shape}, val={val_dl[i][0].shape}, test={test_dl[0][0].shape}')
    
    # < REPORT RELATED CODE > ---------------------------------------------------------------------
    client_distribution = utils.count_labels_per_client(args, train_dl, logger)
    logs_dict_data = defaultdict(list)

    for label in range(test_dl[0][1].unique().shape[0]):
        for dict_key in client_distribution:
            if label in client_distribution[dict_key]:
                logs_dict_data[str(dict_key)].append(client_distribution[dict_key][label])
            else:
                logs_dict_data[str(dict_key)].append(0)

    utils.save_logs(logs_dict_data, os.path.join(args.log_dir, f"data_distribution_{args.tag}.csv"))
    # </ REPORT RELATED CODE > --------------------------------------------------------------------

    if args.fedavg:
        # start fedavg training
        averaging_weights_fl(train_dl, val_dl, test_dl, args, logger)
    else:
        # start ensemble-based training
        ensemble_based_fl(train_dl, val_dl, test_dl, args, logger)