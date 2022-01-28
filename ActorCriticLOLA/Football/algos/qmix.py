import copy
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.distributions import Categorical
import torch.multiprocessing as mp
import numpy as np
from models.mixers.vdn import VDNMixer
from models.mixers.qmix_net import QMixNet
from models.agents.rnn_agent import RNNAgent
from torch.optim import Adam
import pdb


class QLearner():
    def __init__(self, arg_dict, model, mixer, device=None):
        self.gamma = arg_dict["gamma"]
        self.K_epoch = arg_dict["k_epoch"]
        self.lmbda = arg_dict["lmbda"]
        self.eps_clip = arg_dict["eps_clip"]
        self.entropy_coef = arg_dict["entropy_coef"]
        self.grad_clip = arg_dict["grad_clip"]
        self.params = list(model.parameters())
        self.last_target_update_step = 0
        self.optimization_step = 0
        self.arg_dict = arg_dict

        self.n_actions = self.arg_dict["n_actions"]
        self.n_agents = self.arg_dict["n_agents"]
        self.state_shape = self.arg_dict["state_shape"]
        self.obs_shape = self.arg_dict["obs_shape"]
        input_shape = self.obs_shape
        # 根据参数决定RNN的输入维度
        if self.arg_dict["last_action"]:
            input_shape += self.n_actions
        if self.arg_dict["reuse_network"]:
            input_shape += self.n_agents

        # 神经网络
        # device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        device = "cpu"
        # self.eval_rnn = RNNAgent(input_shape, self.arg_dict, device)  # 每个agent选动作的网络
        # self.target_rnn = RNNAgent(input_shape, self.arg_dict, device)

        # self.mixer = None
        # if arg_dict["mixer"] is not None:
        #     if arg_dict["mixer"] == "vdn":
        #         self.mixer = VDNMixer()
        #     elif arg_dict["mixer"] == "qmix":
        #         self.mixer = QMixNet(arg_dict)
        #     else:
        #         raise ValueError("Mixer {} not recognised".format(arg_dict["mixer"]))
        #     self.params += list(self.mixer.parameters())
        #     self.target_mixer = copy.deepcopy(self.mixer)

        # self.optimizer = Adam(params=self.params, lr=arg_dict["learning_rate"])

        self.target_model = copy.deepcopy(model)
        self.target_mixer = copy.deepcopy(mixer)
        self.eval_parameters = list(model.parameters()) + list(mixer.parameters())
        if arg_dict["optimizer"] == "RMS":
            self.optimizer = torch.optim.RMSprop(self.eval_parameters, lr=arg_dict["learning_rate"])

    def split_agents(self, value): # 输入维度：[120,32], 其中120代表2个agent的30个transition，奇数表示agent1，偶数表示agent2
        q_x_1 = torch.Tensor([])
        q_x_2 = torch.Tensor([])
        for i in range(self.arg_dict["rollout_len"]):
            q_a_1 = value[2 * i]  # (12)
            q_a_2 = value[2 * i + 1]
            q_x_1 = torch.cat([q_x_1, q_a_1], dim=0)
            q_x_2 = torch.cat([q_x_2, q_a_2], dim=0)
        return torch.stack((q_x_1, q_x_2), dim=0)  # (2, 60*32)

    def obtain_one_state(self, state): # 输入维度：[120,32,136],其中120代表2个agent的30个transition，奇数表示agent1，偶数表示agent2
        q_x_1 = torch.Tensor([])
        q_x_2 = torch.Tensor([])
        for i in range(self.arg_dict["rollout_len"]):
            q_a_1 = state[2 * i]  # (12)
            q_a_2 = state[2 * i + 1]
            q_x_1 = torch.cat([q_x_1, q_a_1], dim=0)
            q_x_2 = torch.cat([q_x_2, q_a_2], dim=0)
        return q_x_1  # (60,32,136)

    def train(self, model, mixer, data):

        ### rjq debug 0118
        # self.model.load_state_dict(model.state_dict())  # rjq 0114  传入self.model
        # if self.mixer is not None:
        #     self.mixer.load_state_dict(mixer.state_dict())  # mixer.state_dict() == self.target_mixer.state_dict()

        # model.init_hidden()
        # self.target_model.init_hidden()

        loss = []
        for mini_batch in data:
            # pdb.set_trace()
            # obs_model, h_in, actions1, avail_u, actions_onehot1, reward_agency, obs_prime, h_out, avail_u_next, done
            s, h_in, a, a1, avail_u, r, s_prime, h_out, avail_u_next, done = mini_batch
            # print("")
            s = s.reshape(self.arg_dict["rollout_len"] * 2 * self.arg_dict["batch_size"], -1)
            h_in = h_in.reshape(self.arg_dict["rollout_len"] * 2 * self.arg_dict["batch_size"], -1)
            q, _ = model(s, h_in)  #
            q = q.reshape(self.arg_dict["rollout_len"] *2, self.arg_dict["batch_size"], -1)

            s_prime = s_prime.reshape(self.arg_dict["rollout_len"] * 2 * self.arg_dict["batch_size"], -1)
            h_out = h_out.reshape(self.arg_dict["rollout_len"] * 2 * self.arg_dict["batch_size"], -1)
            target_q, _ = self.target_model(s_prime, h_out)
            target_q_ = target_q.reshape(self.arg_dict["rollout_len"] * 2, self.arg_dict["batch_size"], -1)

            q_ = torch.gather(q, dim=2, index=a.long()).squeeze(2)

            target_q_[avail_u_next == 0.0 ] = - 9999999
            target_q_ = target_q_.max(dim=2)[0]



            if mixer is not None:
                if self.arg_dict["mixer"] == "qmix":
                    q_ = self.split_agents(q_).permute(1, 0).reshape(self.arg_dict["rollout_len"],
                                                                     self.arg_dict["batch_size"], -1).permute(1, 0, 2)  # --> (32, 30, 2)
                    target_q_ = self.split_agents(target_q_).permute(1, 0).reshape(self.arg_dict["rollout_len"],
                                                                                   self.arg_dict["batch_size"],
                                                                                   -1).permute(1, 0, 2)  # --> (32, 30, 2)
                    s = s.reshape(self.arg_dict["rollout_len"] * 2, self.arg_dict["batch_size"], -1)
                    s_1 = self.obtain_one_state(s).reshape(self.arg_dict["rollout_len"], self.arg_dict["batch_size"],
                                                           -1).permute(1, 0, 2)  # (batch_size, rollout_len, 136)
                    s_prime = s_prime.reshape(self.arg_dict["rollout_len"] * 2, self.arg_dict["batch_size"], -1)
                    s_prime_1 = self.obtain_one_state(s_prime).reshape(self.arg_dict["rollout_len"],
                                                                       self.arg_dict["batch_size"], -1).permute(1, 0, 2)  # (batch_size, rollout_len, 136)
                    q_total_q_a = mixer(q_, s_1)  # ( 32,30,2) ( 32,30,136)  ### q_total_q_a ( 32,30,1)
                    q_total_target_max_q = self.target_mixer(target_q_, s_prime_1)
                    q_total_target_max_q = q_total_target_max_q.permute(1, 0, 2).reshape(
                        self.arg_dict["rollout_len"] * self.arg_dict["batch_size"], -1).permute(1, 0)
                    q_total_q_a = q_total_q_a.permute(1, 0, 2).reshape(
                        self.arg_dict["rollout_len"] * self.arg_dict["batch_size"], -1).permute(1, 0)

                elif self.arg_dict["mixer"] == "vdn":
                    q_ = self.split_agents(q_)  # [60,5] --> (2, 30*5)
                    target_q_ = self.split_agents(target_q_)  # (2, 30*5)
                    q_total_q_a = mixer(q_)  # (1, 60*32)
                    q_total_target_max_q = self.target_mixer(target_q_)  # (1, 60*32)

            s = self.split_agents((1 - done).squeeze(2))[0].unsqueeze(0)
            r_stack = self.split_agents(r.squeeze(2))  # (60,32) --> (2, 30*32)
            r_total = torch.sum(r_stack, dim=0, keepdim=True)  # (1, 60*32)

            targets = r_total + self.arg_dict["gamma"] * s * (q_total_target_max_q)  # (1, 60*32)

            td_error = (q_total_q_a - targets.detach()).transpose(1,0)  #(1920, 1)
            loss_mini = torch.mean((td_error ** 2))
            loss.append(loss_mini)

        loss = torch.mean(torch.stack(loss), 0)
        # loss = torch.sum(loss)

        # Optimize
        self.optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.eval_parameters, self.grad_clip)
        self.optimizer.step()

        # self.optimization_step += self.arg_dict["batch_size"] * self.arg_dict["buffer_size"] * self.arg_dict["k_epoch"]
        # if (self.optimization_step - self.last_target_update_step) // self.arg_dict["target_update_interval"] >= 1.0:
        #     self._update_targets(model, mixer)
        #     self.last_target_update_step = self.optimization_step
        #     print("self.last_target_update_step:---", self.last_target_update_step)
        self.optimization_step += 1
        if self.optimization_step % self.arg_dict["target_update_interval"] == 0.0:
            self._update_targets(model, mixer)
            self.last_target_update_step = self.optimization_step
            print("self.last_target_update_step:---", self.last_target_update_step)

        return loss


    def _update_targets(self, model, mixer):
        self.target_model.load_state_dict(model.state_dict())
        if mixer is not None:
            self.target_mixer.load_state_dict(mixer.state_dict())

    # def cuda(self):
    #     self.model.cuda()
    #     self.target_model.cuda()
    #     if self.mixer is not None:
    #         self.mixer.cuda()
    #         self.target_mixer.cuda()

    def save_models(self, path, model, mixer):
        torch.save(model.state_dict(), "{}agent.th".format(path))
        if mixer is not None:
            torch.save(mixer.state_dict(), "{}mixer.th".format(path))
        torch.save(self.optimizer.state_dict(), "{}opt.th".format(path))
        print("Model saved :", path)

    # def load_models(self, path):
    #     self.model.load_models(path)
    #     self.target_model.load_models(path)
    #     if self.mixer is not None:
    #         self.mixer.load_state_dict(torch.load("{}/mixer.th".format(path)),
    #                                    map_location=lambda storage, loc: storage)
    #     self.optimizer.load_state_dict(torch.load("{}/opt.th".format(path)), map_location=lambda storage, loc: storage)
